require "json"
require "uri"

module Foo
  class Bar
    def greet(name)
      helper(name)
    end
  end
end

def helper(x)
  x + 1
end

helper(1); helper(2)
