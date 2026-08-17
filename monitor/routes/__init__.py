from .development import register_development_routes
from .files import register_file_routes
from .operations import register_operation_routes
from .sockets import register_socket_routes

__all__ = [
    "register_development_routes",
    "register_file_routes",
    "register_operation_routes",
    "register_socket_routes",
]
