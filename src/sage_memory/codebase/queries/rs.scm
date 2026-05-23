; Rust extraction query.
;
; ``function_item`` covers both top-level fns and methods inside an
; impl block — the extractor decides FUNCTION vs METHOD by looking
; for an enclosing ``impl_item``. ``impl_item`` itself isn't a symbol
; (the struct it impls is) but its ``type`` field anchors the
; method's qualified_name and parent_id linkage.

(function_item) @function
(impl_item) @impl
(struct_item) @struct
(enum_item) @enum
(use_declaration) @use
(call_expression) @call
