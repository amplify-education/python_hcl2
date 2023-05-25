"""A Lark Transformer for transforming a Lark parse tree into a Python dict"""
import logging
import re
import sys
from collections import namedtuple
from typing import List, Dict, Any

from lark.tree import Meta
from lark.visitors import Transformer, Discard, _DiscardType, v_args


HEREDOC_PATTERN = re.compile(r"<<([a-zA-Z][a-zA-Z0-9._-]+)\n([\s\S]*)\1", re.S)
HEREDOC_TRIM_PATTERN = re.compile(r"<<-([a-zA-Z][a-zA-Z0-9._-]+)\n([\s\S]*)\1", re.S)


START_LINE = "__start_line__"
END_LINE = "__end_line__"


Attribute = namedtuple("Attribute", ("key", "value"))


# pylint: disable=missing-function-docstring,unused-argument
class DictTransformer(Transformer):
    """Takes a syntax tree generated by the parser and
    transforms it to a dict.
    """

    with_meta: bool

    def __init__(self, with_meta: bool = False):
        """
        :param with_meta: If set to true then adds `__start_line__` and `__end_line__`
        parameters to the output dict. Default to false.
        """
        self.with_meta = with_meta
        super().__init__()

    def float_lit(self, args: List) -> float:
        return float("".join([str(arg) for arg in args]))

    def int_lit(self, args: List) -> int:
        return int("".join([str(arg) for arg in args]))

    def expr_term(self, args: List) -> Any:
        args = self.strip_new_line_tokens(args)

        #
        if args[0] == "true":
            return True
        if args[0] == "false":
            return False
        if args[0] == "null":
            return None

        # if the expression starts with a paren then unwrap it
        if args[0] == "(":
            return args[1]
        # otherwise return the value itself
        return args[0]

    def index_expr_term(self, args: List) -> str:
        args = self.strip_new_line_tokens(args)
        return f"{args[0]}{args[1]}"

    def index(self, args: List) -> str:
        args = self.strip_new_line_tokens(args)
        return f"[{args[0]}]"

    def get_attr_expr_term(self, args: List) -> str:
        return f"{args[0]}{args[1]}"

    def get_attr(self, args: List) -> str:
        return f".{args[0]}"

    def attr_splat_expr_term(self, args: List) -> str:
        return f"{args[0]}{args[1]}"

    def attr_splat(self, args: List) -> str:
        args_str = "".join(str(arg) for arg in args)
        return f".*{args_str}"

    def full_splat_expr_term(self, args: List) -> str:
        return f"{args[0]}{args[1]}"

    def full_splat(self, args: List) -> str:
        args_str = "".join(str(arg) for arg in args)
        return f"[*]{args_str}"

    def tuple(self, args: List) -> List:
        return [self.to_string_dollar(arg) for arg in self.strip_new_line_tokens(args)]

    def object_elem(self, args: List) -> Dict:
        # This returns a dict with a single key/value pair to make it easier to merge these
        # into a bigger dict that is returned by the "object" function
        key = self.strip_quotes(args[0])
        value = self.to_string_dollar(args[1])

        return {key: value}

    def object(self, args: List) -> Dict:
        args = self.strip_new_line_tokens(args)
        result: Dict[str, Any] = {}
        for arg in args:
            result.update(arg)
        return result

    def function_call(self, args: List) -> str:
        args = self.strip_new_line_tokens(args)
        args_str = ""
        if len(args) > 1:
            args_str = ", ".join([str(arg) for arg in args[1] if arg is not Discard])
        return f"{args[0]}({args_str})"

    def arguments(self, args: List) -> List:
        return args

    def new_line_and_or_comma(self, args: List) -> _DiscardType:
        return Discard

    @v_args(meta=True)
    def block(self, meta: Meta, args: List) -> Dict:
        *block_labels, block_body = args
        result: Dict[str, Any] = block_body
        if self.with_meta:
            result.update(
                {
                    START_LINE: meta.line,
                    END_LINE: meta.end_line,
                }
            )

        # create nested dict. i.e. {label1: {label2: {labelN: result}}}
        for label in reversed(block_labels):
            label_str = self.strip_quotes(label)
            result = {label_str: result}

        return result

    def attribute(self, args: List) -> Attribute:
        key = str(args[0])
        if key.startswith('"') and key.endswith('"'):
            key = key[1:-1]
        value = self.to_string_dollar(args[1])
        return Attribute(key, value)

    def conditional(self, args: List) -> str:
        args = self.strip_new_line_tokens(args)
        return f"{args[0]} ? {args[1]} : {args[2]}"

    def binary_op(self, args: List) -> str:
        return " ".join([str(arg) for arg in args])

    def unary_op(self, args: List) -> str:
        return "".join([str(arg) for arg in args])

    def binary_term(self, args: List) -> str:
        args = self.strip_new_line_tokens(args)
        return " ".join([str(arg) for arg in args])

    def body(self, args: List) -> Dict[str, List]:
        # See https://github.com/hashicorp/hcl/blob/main/hclsyntax/spec.md#bodies
        # ---
        # A body is a collection of associated attributes and blocks.
        #
        # An attribute definition assigns a value to a particular attribute
        # name within a body. Each distinct attribute name may be defined no
        # more than once within a single body.
        #
        # A block creates a child body that is annotated with a block type and
        # zero or more block labels. Blocks create a structural hierarchy which
        # can be interpreted by the calling application.
        # ---
        #
        # There can be more than one child body with the same block type and
        # labels. This means that all blocks (even when there is only one)
        # should be transformed into lists of blocks.
        args = self.strip_new_line_tokens(args)
        attributes = set()
        result: Dict[str, Any] = {}
        for arg in args:
            if isinstance(arg, Attribute):
                if arg.key in result:
                    logging.warning(f"{arg.key} already defined in attribute, ignoring")
                    continue
                result[arg.key] = arg.value
                attributes.add(arg.key)
            else:
                # This is a block.
                for key, value in arg.items():
                    key = str(key)
                    if key in result:
                        if key in attributes:
                            logging.warning(f"{key} already defined in block, ignoring")
                            continue
                        result[key].append(value)
                    else:
                        result[key] = [value]

        return result

    def start(self, args: List) -> Dict:
        args = self.strip_new_line_tokens(args)
        return args[0]

    def binary_operator(self, args: List) -> str:
        return str(args[0])

    def heredoc_template(self, args: List) -> str:
        match = HEREDOC_PATTERN.match(str(args[0]))
        if not match:
            raise RuntimeError(f"Invalid Heredoc token: {args[0]}")

        trim_chars = "\n\t "
        return f'"{match.group(2).rstrip(trim_chars)}"'

    def heredoc_template_trim(self, args: List) -> str:
        # See https://github.com/hashicorp/hcl2/blob/master/hcl/hclsyntax/spec.md#template-expressions
        # This is a special version of heredocs that are declared with "<<-"
        # This will calculate the minimum number of leading spaces in each line of a heredoc
        # and then remove that number of spaces from each line
        match = HEREDOC_TRIM_PATTERN.match(str(args[0]))
        if not match:
            raise RuntimeError(f"Invalid Heredoc token: {args[0]}")

        trim_chars = "\n\t "
        text = match.group(2).rstrip(trim_chars)
        lines = text.split("\n")

        # calculate the min number of leading spaces in each line
        min_spaces = sys.maxsize
        for line in lines:
            leading_spaces = len(line) - len(line.lstrip(" "))
            min_spaces = min(min_spaces, leading_spaces)

        # trim off that number of leading spaces from each line
        lines = [line[min_spaces:] for line in lines]

        return '"%s"' % "\n".join(lines)

    def new_line_or_comment(self, args: List) -> _DiscardType:
        return Discard

    def for_tuple_expr(self, args: List) -> str:
        args = self.strip_new_line_tokens(args)
        for_expr = " ".join([str(arg) for arg in args[1:-1]])
        return f"[{for_expr}]"

    def for_intro(self, args: List) -> str:
        args = self.strip_new_line_tokens(args)
        return " ".join([str(arg) for arg in args])

    def for_cond(self, args: List) -> str:
        args = self.strip_new_line_tokens(args)
        return " ".join([str(arg) for arg in args])

    def for_object_expr(self, args: List) -> str:
        args = self.strip_new_line_tokens(args)
        for_expr = " ".join([str(arg) for arg in args[1:-1]])
        # doubled curly braces stands for inlining the braces
        # and the third pair of braces is for the interpolation
        # e.g. f"{2 + 2} {{2 + 2}}" == "4 {2 + 2}"
        return f"{{{for_expr}}}"

    def strip_new_line_tokens(self, args: List) -> List:
        """
        Remove new line and Discard tokens.
        The parser will sometimes include these in the tree so we need to strip them out here
        """
        return [arg for arg in args if arg != "\n" and arg is not Discard]

    def to_string_dollar(self, value: Any) -> Any:
        """Wrap a string in ${ and }"""
        if isinstance(value, str):
            if value.startswith('"') and value.endswith('"'):
                return str(value)[1:-1]
            return f"${{{value}}}"
        return value

    def strip_quotes(self, value: Any) -> Any:
        """Remove quote characters from the start and end of a string"""
        if isinstance(value, str):
            if value.startswith('"') and value.endswith('"'):
                return str(value)[1:-1]
        return value

    def identifier(self, value: Any) -> Any:
        # Making identifier a token by capitalizing it to IDENTIFIER
        # seems to return a token object instead of the str
        # So treat it like a regular rule
        # In this case we just convert the whole thing to a string
        return str(value[0])
