; TypeScript extraction query.
;
; Captures candidate node types; symbol kind / qualified_name /
; parent_id linkage is computed in the extractor by walking the
; captured node's parent chain (uniform across the TS/TSX/JS family).

(function_declaration) @function
(method_definition) @method
(class_declaration) @class
(interface_declaration) @interface
(enum_declaration) @enum

; Named arrow function or function expression — `const x = () => ...`
; produces a FUNCTION symbol with name `x`. Anonymous arrow functions
; (callbacks etc.) are intentionally not captured.
(lexical_declaration
  (variable_declarator
    name: (identifier) @var_name
    value: [(arrow_function) (function_expression)] @var_value)) @var_decl

(import_statement) @import
(call_expression) @call
