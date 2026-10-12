"""固定版本的内置工具注册表。"""
REGISTRY={}
def register(cls):
    REGISTRY[cls.name]=cls()
    return cls

def get(name):
    from . import files
    return REGISTRY[name]
