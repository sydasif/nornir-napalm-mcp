"""Core kernel — shared stateless services and the CoreBase class.

Nothing above ``core`` may import back into server.py or cli/. Import
services from their submodules directly (``from nornir_mcp.core import
errors``) rather than via package-level re-exports.
"""
