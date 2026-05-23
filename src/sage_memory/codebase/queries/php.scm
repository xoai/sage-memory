; PHP extraction query.
;
; PHP distinguishes three call node types — function_call_expression
; (``f()``), member_call_expression (``$obj->m()``), and
; scoped_call_expression (``A::b()``). The extractor handles each.
; namespace_use_declaration → import relations (the qualified_name
; child carries the target). Top-level ``namespace App;`` declarations
; are NOT emitted as symbols and do not prefix qnames (matching
; Java/C++ convention in this codebase).

(class_declaration) @class
(interface_declaration) @interface
(method_declaration) @method
(function_definition) @function
(namespace_use_declaration) @use
(function_call_expression) @call
(member_call_expression) @member_call
(scoped_call_expression) @scoped_call
