import { foo } from "./bar";
import * as ns from "lib";

function helper(x) { return x + 1; }

class Greeter {
  greet(name) { return helper(1); }
}

const arrow = (x) => helper(x);

function outer() { function inner_helper() { return 1; } }
function outer() { function inner_helper() { return 2; } }

helper(1); helper(2);
