; TSX extraction query.
;
; The TSX grammar is a superset of TypeScript that also parses JSX —
; same set of TS symbol/import/call node types apply. JSX-specific
; nodes (jsx_element, jsx_self_closing_element, etc.) are not symbol
; sources; calls inside JSX expression containers surface through the
; same `call_expression` rule below.

(function_declaration) @function
(method_definition) @method
(class_declaration) @class
(interface_declaration) @interface
(enum_declaration) @enum

(lexical_declaration
  (variable_declarator
    name: (identifier) @var_name
    value: [(arrow_function) (function_expression)] @var_value)) @var_decl

(import_statement) @import
(call_expression) @call
