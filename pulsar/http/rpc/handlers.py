import sys
import logging

from pulsar import PickableMixin
from pulsar.utils.tools import checkarity

from .exceptions import NoSuchFunction, InvalidParams

__all__ = ['RpcHandler']


class RpcResponse(object):
    __slots__ = ('handler','path','func')
    
    def __init__(self, handler, path, func):
        self.handler = handler
        self.path = path
        self.func = func
    
    @property
    def name(self):
        return self.func.__name__
    
    @property
    def content_type(self):
        return self.handler.content_type
    
    def __repr__(self):
        return self.func.__name__
        
    def info(self, request, id, msg, ok = True):
        '''Do something with the message and request'''
        self.handler.log.info(msg)
        
    def critical(self, request, id, e):
        msg = 'Unhandled server exception %s: %s' % (e.__class__.__name__,e)
        self.handler.log.critical(msg,exc_info=sys.exc_info)
        raise InternalError(msg)
    
    def __call__(self, request, id, version, *args, **kwargs):
        try:
            if not self.func:
                msg = 'Function "{0}" not available.'.format(self.path)
                self.info(request,id,msg,False)
                raise NoSuchFunction(msg)
            try:
                deco = self.handler.wrap_function_decorator
                result = deco(self, request, *args, **kwargs)
            except TypeError as e:
                msg = checkarity(self.func,args,kwargs,discount=2)
                if msg:
                    self.info(request,id,'Invalid Parameters in rpc function: {0}'.format(msg),False)
                    raise InvalidParams(msg)
                else:
                    self.critical(request,id,e)
            except Exception as e:
                self.critical(request,id,e)
            result = self.handler.dumps(id,version,result=result)
            self.info(request,id,'Successfully handled rcp function "{0}"'.format(self.path))
            return result
        except Exception as e:
            return self.handler.dumps(id,version,error=e)
            
    

class MetaRpcHandler(type):
    '''A metaclass for rpc handlers. Add a limited ammount of magic to RPC handlers.'''
    def __new__(cls, name, bases, attrs):
        make = super(MetaRpcHandler, cls).__new__
        if attrs.pop('virtual',None):
            return make(cls,name,bases,attrs)
        
        funcprefix = attrs.get('serve_as',None)
        if not funcprefix:
            for base in bases[::-1]:
                if isinstance(base, MetaRpcHandler):
                    funcprefix = base.serve_as
                    if funcprefix:
                        break
        
        rpc = {}
        if funcprefix:
            fprefix = '%s_' % funcprefix
            for key, method in list(attrs.items()):
                if hasattr(method,'__call__') and key.startswith(fprefix):
                    namefunc = key[len(fprefix):]
                    func = attrs.pop(key)
                    func.__name__ = namefunc
                    rpc[namefunc] = func
            for base in bases[::-1]:
                if hasattr(base, 'rpcfunctions'):
                    rpcbase = base.rpcfunctions
                    for key,method in rpcbase.items():
                        if not rpc.has_key(key):
                            rpc[key] = method
                        
        attrs['rpcfunctions'] = rpc
        return make(cls, name, bases, attrs)


BaseHandler = MetaRpcHandler('BaseRpcHandler',(object,),{'virtual':True})


class RpcHandler(BaseHandler,PickableMixin):
    '''Server Handler.
Sub-handlers for prefixed methods (e.g., system.listMethods)
can be added with putSubHandler. By default, prefixes are
separated with a '.'. Override self.separator to change this.
    '''
    route        = '/'
    serve_as     = 'rpc'
    '''Type of server and prefix to functions providing services'''
    separator    = '.'
    content_type = 'text/plain'
    '''Separator between subhandlers.'''
    RESPONSE     = RpcResponse

    def __init__(self,
                 subhandlers = None,
                 http = None,
                 attrs = None,
                 route = None,
                 **kwargs):
        self.route = route if route is not None else self.route
        self.subHandlers = {}
        self.log = self.getLogger(**kwargs)
        if subhandlers:
            for route,handler in subhandlers.items():
                if inspect.isclass(handler):
                    handler = handler(http = self.http)
                self.putSubHandler(route, handler)
    
    def get_method_and_args(self, data):
        raise NotImplementedError
    
    def __getitem__(self, path):
        return self._getFunction(path)
    
    def get_handler(self, path):
        prefixes = path.split(self.separator)
        return self._get_handler(prefixes)
    
    def _get_handler(self, prefixes):
        handler = self
        for path in prefixes:
            handler = handler.getSubHandler(path)
            if not handler:
                raise NoSuchFunction('Could not find path {0}'.format(path))
        return handler
            
    def putSubHandler(self, prefix, handler):
        '''Add a subhandler with prefix prefix
        
:keyword prefix: a string defining the url of the subhandler
:keyword handler: the sub-handler, an instance of :class:`Handler` 
        '''
        self.subHandlers[prefix] = handler

    def getSubHandler(self, prefix):
        '''Get a subhandler at ``prefix``
        '''
        return self.subHandlers.get(prefix, None)
    
    def wrap_function_decorator(self, rpc, *args, **kwargs):
        return rpc.func(self, *args,**kwargs)
    
    def _getFunction(self, method):
        prefix_method = method.split(self.separator, 1)
        if len(prefix_method) > 1:
            # Found prefixes, get the subhandler
            method = prefix_method[-1]
            handler = self._get_handler(prefix_method[:-1])
        else:
            handler = self
        try:
            func = handler.rpcfunctions[method]
        except:
            func = None
        return self.RESPONSE(handler,method,func)

    def invokeServiceEndpoint(self, meth, args):
        return meth(*args)

    def listFunctions(self):
        return self.rpcfunctions.keys()
    
    def __call__(self, environ, start_response):
        '''The WSGI handler which consume the remote procedure call'''
        status = '200 OK'
        path = environ['PATH_INFO']
        data = environ['wsgi.input'].read()
        method, args, kwargs, id, version = self.get_method_and_args(data)
        rpc_handler = self._getFunction(method)
        result = rpc_handler(environ, id, version, *args, **kwargs)
        response_headers = (
                            ('Content-type',rpc_handler.content_type),
                            ('Content-Length', str(len(result)))
                            )
        start_response(status, response_headers)
        return [result]
        
        