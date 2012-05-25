#!/usr/bin/python
# ----------------------------------------------------------------------------
# Simple regular expression that obtains super class and protocols from Obj-C
# interfaces
#
# Author: Ricardo Quesada
# Copyright 2012 (C) Zynga, Inc
#
# Dual License: MIT or GPL v2.
# ----------------------------------------------------------------------------
'''
Obtains 
'''

__docformat__ = 'restructuredtext'


# python
import sys
import os
import re
import getopt
import glob
import ast
import xml.etree.ElementTree as ET
import itertools
import copy
import datetime

BINDINGS_PREFIX = 'js_bindings_'
PROXY_PREFIX = 'JSPROXY_'

#
# Templates
#
autogenerated_template = '''/*
* AUTOGENERATED FILE. DO NOT EDIT IT
* Generated by %s on %s
*/
'''

import_template = '''
// needed for callbacks from objective-c to JS
#import <objc/runtime.h>
#import "JRSwizzle.h"

#import "jstypedarray.h"
#import "ScriptingCore.h"   

#import "%s.h"

'''



# xml2d recipe copied from here:
# http://code.activestate.com/recipes/577722-xml-to-python-dictionary-and-back/
def xml2d(e):
    """Convert an etree into a dict structure

    @type  e: etree.Element
    @param e: the root of the tree
    @return: The dictionary representation of the XML tree
    """
    def _xml2d(e):
        kids = dict(e.attrib)
        for k, g in itertools.groupby(e, lambda x: x.tag):
            g = [ _xml2d(x) for x in g ] 
            kids[k]=  g
        return kids
    return { e.tag : _xml2d(e) }


class SpiderMonkey(object):
    def __init__(self, bridgesupport_file, hierarchy_file, classes_to_bind=[] ):
        self.bridgesupport_file = bridgesupport_file
        self.bs = {}

        self.hierarchy_file = hierarchy_file
        self.hierarchy = {}
        
        self.classes_to_bind = set(classes_to_bind)

    def parse_hierarchy_file( self ):
        f = open( self.hierarchy_file )
        self.hierarchy = ast.literal_eval( f.read() )
        f.close()

    def parse_bridgesupport_file( self ):
        p = ET.parse( self.bridgesupport_file )
        root = p.getroot()
        self.bs = xml2d( root )

    def ancestors( self, klass, list_of_ancestors ):
        if klass not in self.hierarchy:
            return list_of_ancestors

        info = self.hierarchy[ klass ]
        subclass =  info['subclass']
        if not subclass:
            return list_of_ancestors

        list_of_ancestors.append( subclass )

        return self.ancestors( subclass, list_of_ancestors )

    #
    # Helper
    #
    def convert_selector_name_to_native( self, name ):
        return name.replace(':','_')

    def convert_selector_name_to_js( self, name ):
        return name.replace(':','')

    #
    # "class" constructor and destructor
    #
    def generate_constructor( self, class_name ):

        # Global Variables
        # 1: JSPROXY_CCNode  2: JSPROXY_CCNode
        constructor_globals = '''
JSClass* %s_class = NULL;
JSObject* %s_object = NULL;
'''

        # 1: JSPROXY_CCNode,
        # 2: JSPROXY_CCNode, 3: JSPROXY_CCNode
        # 4: CCNode, 5: CCNode
        # 6: JSPROXY_CCNode,  7: JSPROXY_CCNode
        # 8: possible callback code
        constructor_template = ''' // Constructor
JSBool %s_constructor(JSContext *cx, uint32_t argc, jsval *vp)
{
    JSObject *jsobj = JS_NewObject(cx, %s_class, %s_object, NULL);
    %s *realObj = [%s alloc];

    %s *proxy = [[%s alloc] initWithJSObject:jsobj andRealObject:realObj];

    [realObj release];

    JS_SetPrivate(jsobj, proxy);
    JS_SET_RVAL(cx, vp, OBJECT_TO_JSVAL(jsobj));

    %s
    
    return JS_TRUE;
}
'''
        proxy_class_name = '%s%s' % (PROXY_PREFIX, class_name )
        self.mm_file.write( constructor_globals % ( proxy_class_name, proxy_class_name ) )
        self.mm_file.write( constructor_template % ( proxy_class_name, proxy_class_name, proxy_class_name, class_name, class_name, proxy_class_name, proxy_class_name, '/* no callbacks */' ) )

    def generate_destructor( self, class_name ):
        # 1: JSPROXY_CCNode,
        # 2: JSPROXY_CCNode, 3: JSPROXY_CCNode
        # 4: possible callback code
        destructor_template = '''
// Destructor
void %s_finalize(JSContext *cx, JSObject *obj)
{
	%s *pt = (%s*)JS_GetPrivate(obj);
	if (pt) {
		// id real = [pt realObj];
	
		%s

		[pt release];

		JS_free(cx, pt);
	}
}
'''
        proxy_class_name = '%s%s' % (PROXY_PREFIX, class_name )
        self.mm_file.write( destructor_template % ( proxy_class_name, proxy_class_name, proxy_class_name, '/* no callbacks */' ) )

    #
    # Method generator functions
    #
    def generate_call_to_real_object( self, selector_name, num_of_args, ret_declared_type, args_declared_type ):
        prefix = ''
        if ret_declared_type:
            prefix = 'ret_val = '

        args = selector_name.split(':')
        call = prefix + '[real '

        # sanity check
        if num_of_args+1 != len(args):
            raise Exception('Error parsing...')


        for i,arg in enumerate(args):
            if num_of_args == 0:
                call += arg
            elif arg:   # empty arg?
                # cast needed to prevent compiler errors
                call += '%s:(%s)arg%d ' % (arg, args_declared_type[i], i)

        call += ' ];';

        return call
            
    def generate_return_string( self, declared_type, js_type ):
        convert = {
            'i' : 'INT_TO_JSVAL(ret_val)',
            'u' : 'INT_TO_JSVAL(ret_val)',
            'b' : 'BOOLEAN_TO_JSVAL(ret_val)',
            'o' : 'OBJECT_TO_JSVAL(ret_val)',
            's' : 'STRING_TO_JSVAL(ret_val)',
            'd' : 'DOUBLE_TO_JSVAL(ret_val)',
            'c' : 'INT_TO_JSVAL(ret_val)',
#            'f' : 'FUNCTION_TO_JSVAL(ret_val)',
            None : 'JSVAL_TRUE',
            }
        if js_type not in convert:
            raise Exception("Invalid key: %s" % js_type )

        s = convert[ js_type ]
        return '\tJS_SET_RVAL(cx, vp, %s);' % s

    def parse_method_arguments_and_retval( self, method ):
        # Left column: BridgeSupport types
        # Right column: JS types
        supported_types = {
            'f' : 'd',  # float
            'd' : 'd',  # double
            'i' : 'i',  # integer
            'I' : 'u',  # unsigned integer
            'c' : 'c',  # char
            'C' : 'c',  # unsigned char
            'B' : 'b',  # BOOL
            'v' :  None,  # void (for retval)
            }

        supported_declared_types = { 
            'NSString*' : 'S',
            }

        s = method['selector']

        args_js_type = []
        args_declared_type = []
        ret_js_type = None
        ret_declared_type = None

        found = True

        # parse arguments
        if 'arg' in method:
            args = method['arg']
            for arg in args:
                t = arg['type']
                dt = arg['declared_type']
                if t in supported_types:
                    args_js_type.append( supported_types[t] )
                    args_declared_type.append( dt )
                elif dt in supported_declared_types:
                    args_js_type.append( supported_declared_types[dt] )
                    args_declared_type.append( dt )
                else:
                    found = False
                    break

        if not found:
            return (None, None, None, None)


        # parse ret value
        if 'retval' in method:
            retval = method['retval']
            t = retval[0]['type']
            dt = retval[0]['declared_type']

            # Special case for -(id) initXXX methods
            if s.startswith('init') and dt == 'id':
                ret_js_type = None
                ret_declared_type = None
             
            # Part of supported types ?
            elif t in supported_types:
                if supported_types[t] == None:  # void type
                    ret_js_type = None
                    ret_declared_type = None
                else:
                    ret_js_type = supported_types[t]
                    ret_declared_type = retval[0]['declared_type']

            # Part of supported declared types ?
            elif dt in supported_declared_types:
                ret_js_type.append( supported_declared_types[t] )
                ret_declared_type.append( dt )
            else:
                found = False

        if not found:
            return (None, None, None, None)

        return (args_js_type, args_declared_type, ret_js_type, ret_declared_type )

    # Special case for string to NSString generator
    def generate_argument_string( self, i, arg_js_type ):
        self.mm_file.write( '\tJSString *tmp_arg%d = JS_ValueToString( cx, vp[%d] );\n\tNSString *arg%d = [NSString stringWithUTF8String: JS_EncodeString(cx, tmp_arg%d)];\n' % ( i, i+2, i, i ) )

    def generate_method( self, class_name, method ):

        method_description = '''
// Arguments: %s
// Ret value: %s'''

        # JSPROXY_CCNode, setPosition
        # CCNode
        # CCNode, CCNode
        # 1  (number of arguments)
        method_template = '''
JSBool %s_%s(JSContext *cx, uint32_t argc, jsval *vp) {
	
	JSObject* obj = (JSObject *)JS_THIS_OBJECT(cx, vp);
	JSPROXY_NSObject *proxy = (JSPROXY_NSObject*) JS_GetPrivate( obj );
	NSCAssert( proxy, @"Invalid Proxy object");
	NSCAssert( [proxy isInitialized], @"Object not initialzied. error");
	
	%s * real = (%s*)[proxy realObj];
	NSCAssert( real, @"Invalid real object");

	NSCAssert( argc == %d, @"Invalid number of arguments" );
'''

        return_template = '''
        '''

        end_template = '''
	return JS_TRUE;
}
'''
        # b      JSBool          Boolean
        # c      uint16_t/jschar ECMA uint16_t, Unicode char
        # i      int32_t         ECMA int32_t
        # u      uint32_t        ECMA uint32_t
        # j      int32_t         Rounded int32_t (coordinate)
        # d      double          IEEE double
        # I      double          Integral IEEE double
        # S      JSString *      Unicode string, accessed by a JSString pointer
        # W      jschar *        Unicode character vector, 0-terminated (W for wide)
        # o      JSObject *      Object reference
        # f      JSFunction *    Function private
        # v      jsval           Argument value (no conversion)
        # *      N/A             Skip this argument (no vararg)
        # /      N/A             End of required arguments
        # More info:
        # https://developer.mozilla.org/en/SpiderMonkey/JSAPI_Reference/JS_ConvertArguments
        js_types_conversions = {
            'b' : ['JSBool',    'JS_ValueToBoolean'],
            'd' : ['double',    'JS_ValueToNumber'],
            'I' : ['double',    'JS_ValueToNumber'],    # double converted to string
            'i' : ['int32_t',   'JS_ValueToECMAInt32'],
            'j' : ['int32_t',   'JS_ValueToECMAInt32'],
            'u' : ['uint32_t',  'JS_ValueToECMAUint32'],
            'c' : ['uint16_t',  'JS_ValueToUint16'],
            's' : ['char*',     'XXX'],
            'o' : ['JSObject*', 'XXX'],
            }

        js_special_type_conversions =  {
            'S' : self.generate_argument_string,
        }

        args_js_type, args_declared_type, ret_js_type, ret_declared_type = self.parse_method_arguments_and_retval( method )

        if args_js_type == None:
            print 'NOT OK:' + method['selector']
            return False
       
        s = method['selector']

        # writing...
        converted_name = self.convert_selector_name_to_native( s )

        num_of_args = len( args_declared_type )
        self.mm_file.write( method_description % ( ', '.join(args_declared_type), ret_declared_type ) )

        self.mm_file.write( method_template % ( PROXY_PREFIX+class_name, converted_name, class_name, class_name, num_of_args ) )

        for i,arg in enumerate(args_js_type):

            # XXX: Hack. This "+2" seems to do the trick... don't know why.
            # XXX: FRAGILE CODE. MY BREAK IN FUTURE VERSIONS OF SPIDERMONKEY
            if arg in js_types_conversions:
                t = js_types_conversions[arg]
                self.mm_file.write( '\t%s arg%d; %s( cx, vp[%d], &arg%d );\n' % ( t[0], i, t[1], i+2, i ) )
            elif arg in js_special_type_conversions:
                js_special_type_conversions[arg]( i, arg )
            else:
                raise Exception('Unsupported type: %s' % arg )

        if ret_declared_type:
            self.mm_file.write( '\t%s ret_val;\n' % ret_declared_type )

        call_real = self.generate_call_to_real_object( s, num_of_args, ret_declared_type, args_declared_type )

        self.mm_file.write( '\n\t%s\n' % call_real )

        ret_string = self.generate_return_string( ret_declared_type, ret_js_type )
        self.mm_file.write( ret_string )

        self.mm_file.write( end_template )

        return True

    def generate_methods( self, class_name, klass ):
        ok_methods = []
        for m in klass['method']:
            ok = self.generate_method( class_name, m )
            if ok:
                ok_methods.append( m )
        return ok_methods


    def generate_header( self, class_name, parent_name ):
        # js_bindindings_CCNode
        # js_bindindings_NSObject
        # JSPROXXY_CCNode
        # JSPROXY_CCNode, JSPROXY_NSObject
        # callback code
        header_template = '''
#import "%s.h"

#import "%s.h"

extern JSObject *%s_object;

/* Proxy class */
@interface %s : %s
{
}
'''
        header_template_end = '''
@end
'''
        proxy_class_name = '%s%s' % (PROXY_PREFIX, class_name )

        # Header file
        self.h_file.write( autogenerated_template % ( sys.argv[0], datetime.date.today() ) )

        self.h_file.write( header_template % (  BINDINGS_PREFIX + class_name, BINDINGS_PREFIX + parent_name, proxy_class_name, proxy_class_name, PROXY_PREFIX + parent_name  ) )
        # callback code should be added here
        self.h_file.write( header_template_end )

    def generate_implementation( self, class_name, parent_name, ok_methods ):
        # 1-12: JSPROXY_CCNode
        implementation_template = '''
+(void) createClassWithContext:(JSContext*)cx object:(JSObject*)globalObj name:(NSString*)name
{
	%s_class = (JSClass *)calloc(1, sizeof(JSClass));
	%s_class->name = [name UTF8String];
	%s_class->addProperty = JS_PropertyStub;
	%s_class->delProperty = JS_PropertyStub;
	%s_class->getProperty = JS_PropertyStub;
	%s_class->setProperty = JS_StrictPropertyStub;
	%s_class->enumerate = JS_EnumerateStub;
	%s_class->resolve = JS_ResolveStub;
	%s_class->convert = JS_ConvertStub;
	%s_class->finalize = %s_finalize;
	%s_class->flags = JSCLASS_HAS_PRIVATE;
'''

        # Properties
        properties_template = '''
	static JSPropertySpec properties[] = {
		{0, 0, 0, 0, 0}
	};
'''
        functions_template_start = '''
	static JSFunctionSpec funcs[] = {
'''
        functions_template_end = '\t\tJS_FS_END\n\t};\n'

        static_functions_template = '''
	static JSFunctionSpec st_funcs[] = {
		JS_FS_END
	};
'''
        # 1: JSPROXY_CCNode
        # 2: JSPROXY_NSObject
        # 3-4: JSPROXY_CCNode
        init_class_template = '''
	%s_object = JS_InitClass(cx, globalObj, %s_object, %s_class, %s_constructor,0,properties,funcs,NULL,st_funcs);
}
'''
        proxy_class_name = '%s%s' % (PROXY_PREFIX, class_name )
        proxy_parent_name = '%s%s' % (PROXY_PREFIX, parent_name )

        self.mm_file.write( '\n@implementation %s\n' % proxy_class_name )

        self.mm_file.write( implementation_template % ( proxy_class_name, proxy_class_name, proxy_class_name,
                                                        proxy_class_name, proxy_class_name, proxy_class_name, 
                                                        proxy_class_name, proxy_class_name, proxy_class_name, 
                                                        proxy_class_name, proxy_class_name, proxy_class_name ) )

        self.mm_file.write( properties_template )
        self.mm_file.write( functions_template_start )

        js_fn = '\t\tJS_FN("%s", %s, 1, JSPROP_PERMANENT | JSPROP_SHARED),\n'
        for method in ok_methods:
            js_name = self.convert_selector_name_to_js( method['selector'] )
            cb_name = self.convert_selector_name_to_native( method['selector'] )
            self.mm_file.write( js_fn % (js_name, proxy_class_name + '_' + cb_name) )

        self.mm_file.write( functions_template_end )
        self.mm_file.write( static_functions_template )
        self.mm_file.write( init_class_template % ( proxy_class_name, proxy_parent_name, proxy_class_name, proxy_class_name ) )

        self.mm_file.write( '\n@end\n' )
    
    def generate_class_binding( self, class_name ):

        self.h_file = open( '%s%s.h' % ( BINDINGS_PREFIX, class_name), 'w' )
        self.mm_file = open( '%s%s.mm' % (BINDINGS_PREFIX, class_name), 'w' )

        signatures = self.bs['signatures']
        classes = signatures['class']
        klass = None

        parent_name = self.hierarchy[ class_name ]['subclass']

        # XXX: Super slow. Add them into a dictionary
        for c in classes:
            if c['name'] == class_name:
                klass = c
                break

        methods = klass['method']

        proxy_class_name = '%s%s' % (PROXY_PREFIX, class_name )


        self.generate_header( class_name, parent_name )

        # Implementation file
        self.mm_file.write( autogenerated_template % ( sys.argv[0], datetime.date.today() ) )
        self.mm_file.write( import_template % (BINDINGS_PREFIX+class_name) )

        self.generate_constructor( class_name )
        self.generate_destructor( class_name )

        ok_methods = self.generate_methods( class_name, klass )

        self.generate_implementation( class_name, parent_name, ok_methods )

        self.h_file.close()
        self.mm_file.close()

    def generate_bindings( self ):
        ancestors = []
        for klass in self.classes_to_bind:
            new_list = self.ancestors( klass, [klass] )      
            ancestors.extend( new_list )

        s = set(ancestors)

        # Explicity remove NSObject. It is generated manually
        copy_set = copy.copy(s)
        for i in copy_set:
            if i.startswith('NS'):
                print 'Removing %s from bindings...' % i
                s.remove( i )

        for klass in s:
            self.generate_class_binding( klass )

    def parse( self ):
        self.parse_hierarchy_file()
        self.parse_bridgesupport_file()

        self.generate_bindings()

def help():
    print "%s v1.0 - An utility to generate SpiderMonkey JS bindings for BridgeSupport files" % sys.argv[0]
    print "Usage:"
    print "\t-b --bridgesupport\tBridgesupport file to parse"
    print "\t-j --hierarchy\tFile that contains the hierarchy class and used protocols"
    print "{class to parse}\tName of the classes to generate. If no classes are "
    print "\nExample:"
    print "\t%s -b cocos2d-mac.bridgesupport -j cocos2d-mac_hierarchy.txt CCNode CCSprite" % sys.argv[0]
    sys.exit(-1)

if __name__ == "__main__":
    if len( sys.argv ) == 1:
        help()

    bridgesupport_file = None
    hierarchy_file = None

    argv = sys.argv[1:]
    try:                                
        opts, args = getopt.getopt(argv, "b:j:", ["bridgesupport=","hierarchy="])

        for opt, arg in opts:
            if opt in ("-b","--bridgesupport"):
                bridgesupport_file = arg
            if opt in  ("-j", "--hierarchy"):
                hierarchy_file = arg
    except getopt.GetoptError,e:
        print e
        opts, args = getopt.getopt(argv, "", [])

    if args == None:
        help()

    instance = SpiderMonkey(bridgesupport_file, hierarchy_file, args )
    instance.parse()

