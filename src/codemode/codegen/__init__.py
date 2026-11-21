"""Codegen - pluggable per bahasa target."""

from codemode.codegen import typescript

RENDERERS = {"typescript": typescript.render}

__all__ = ["RENDERERS", "typescript"]
