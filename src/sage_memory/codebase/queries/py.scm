; Python extraction query.
;
; Captures candidate node types for the extractor. Symbol kind
; disambiguation (METHOD vs FUNCTION) is performed by the extractor by
; walking the captured node's parent chain — pure S-expression
; predicates aren't expressive enough across all 10 languages, so the
; extractor owns that logic uniformly.

(function_definition) @function
(class_definition) @class

(import_statement) @import
(import_from_statement) @import_from

(call) @call
