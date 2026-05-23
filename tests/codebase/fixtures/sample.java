package com.example;

import java.util.List;
import java.util.Map;

class Greeter {
    public String greet(String name) { return helper(1); }
    public String helper(int x) { return "hi"; }
}

interface Animal { String name(); }
enum Color { RED, GREEN, BLUE }

class Main {
    public static void main(String[] args) {
        Greeter g = new Greeter();
        g.greet("world");
    }
}
