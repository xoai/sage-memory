; C++ extraction query.
;
; Same surface as C plus ``class_specifier`` (C++ classes). A
; ``function_definition`` inside a class_specifier body is a METHOD;
; outside, it's a FUNCTION. Namespaces are NOT emitted as symbols
; and do not prefix qnames.

(function_definition) @function
(class_specifier) @class
(struct_specifier) @struct
(preproc_include) @include
(call_expression) @call
