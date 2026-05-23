; Go extraction query.
;
; Go's tree-sitter grammar exposes function_declaration and
; method_declaration as separate node types — the method node carries
; the receiver as a field, which the extractor unwraps to compute the
; ``Type.method`` qualified_name.
;
; ``type_declaration`` is general (covers struct/interface/alias) and
; is unwrapped in the extractor to emit only the STRUCT case for v1.

(function_declaration) @function
(method_declaration) @method
(type_declaration) @type_decl
(import_declaration) @import
(call_expression) @call
