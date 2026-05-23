#include <stdio.h>
#include "local.h"

struct Point {
    int x;
    int y;
};

int helper(int x) { return x + 1; }

int main() {
    helper(1);
    helper(2);
    return 0;
}
