; Java extraction query.
;
; Java uses ``method_invocation`` (NOT call_expression) for call
; sites — the extractor handles that distinction. ``method_declaration``
; always appears inside a class/interface/enum body so kind is always
; METHOD; the enclosing declaration provides the parent_qname.

(class_declaration) @class
(interface_declaration) @interface
(enum_declaration) @enum
(method_declaration) @method
(import_declaration) @import
(method_invocation) @call
