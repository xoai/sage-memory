; C extraction query.
;
; Plain C has no classes/methods/interfaces — only structs and
; functions. Includes (``#include <x>`` and ``#include "x"``) become
; import relations.

(function_definition) @function
(struct_specifier) @struct
(preproc_include) @include
(call_expression) @call
