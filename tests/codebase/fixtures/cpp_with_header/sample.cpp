#include <iostream>
#include "sample.h"

class Point {
public:
    int distance() const { return helper(1); }
};

int helper(int x) { return x + 1; }

int main() {
    helper(1);
    helper(2);
    return 0;
}
