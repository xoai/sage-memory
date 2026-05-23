; Ruby extraction query.
;
; Ruby has no dedicated import node — ``require "x"`` is a regular
; ``call`` whose method name happens to be ``require``. The extractor
; detects that pattern. The ``module`` scope is not emitted as a
; symbol (no MODULE in the spec's kind enum) but classes / methods
; defined inside it are captured normally.

(method) @method
(class) @class
(call) @call
