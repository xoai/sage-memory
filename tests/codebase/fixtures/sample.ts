import { foo } from "./bar";
import * as ns from "lib";

function helper(x: number): number { return x + 1; }

class Greeter {
  greet(name: string): string { return helper(1); }
}

interface Animal { name: string; }
const arrow = (x: number) => helper(x);

function outer() { function inner_helper() { return 1; } }
function outer() { function inner_helper() { return 2; } }

helper(1); helper(2);
