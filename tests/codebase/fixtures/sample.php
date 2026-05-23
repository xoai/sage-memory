<?php
namespace App\Service;

use App\Repo\UserRepo;
use App\Util\Foo;

class Greeter {
    public function greet(string $name): string { return $this->helper(1); }
    public function helper(int $x): string { return "hi"; }
}

interface Animal { public function name(): string; }

function helper(int $x): int { return $x + 1; }

helper(1);
helper(2);
