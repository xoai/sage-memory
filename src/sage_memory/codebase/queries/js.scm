; JavaScript extraction query.
;
; Mirror of ts.scm but omits the TS-only node types
; (interface_declaration, enum_declaration) because the javascript
; grammar does not define them — querying for them would fail to
; compile. The js query is also used for `.jsx` files against the tsx
; grammar (walker output: grammar=tsx, query_id=js) so it must stay
; restricted to nodes that exist in plain JavaScript.

(function_declaration) @function
(method_definition) @method
(class_declaration) @class

(lexical_declaration
  (variable_declarator
    name: (identifier) @var_name
    value: [(arrow_function) (function_expression)] @var_value)) @var_decl

(import_statement) @import
(call_expression) @call
