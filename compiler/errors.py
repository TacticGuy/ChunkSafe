"""Compile-time error hierarchy. Every error carries a source location."""


class CompileError(Exception):
    def __init__(self, message, line=None, col=None):
        self.message = message
        self.line = line
        self.col = col
        super().__init__(str(self))

    def __str__(self):
        loc = f"{self.line}:{self.col}: " if self.line is not None else ""
        return f"{type(self).__name__}: {loc}{self.message}"


class ParseError(CompileError):
    pass


class SemanticError(CompileError):
    pass


class UnsafeRetryError(CompileError):
    pass


class SizingError(CompileError):
    pass


class CodegenError(CompileError):
    pass
