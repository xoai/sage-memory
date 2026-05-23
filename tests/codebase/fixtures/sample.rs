use std::collections::HashMap;

pub struct Point { x: i32, y: i32 }

impl Point {
    pub fn new(x: i32, y: i32) -> Self { Point { x, y } }
    pub fn distance(&self) -> f64 { 0.0 }
}

enum Color { Red, Green, Blue }

fn helper(x: i32) -> i32 { x + 1 }

fn main() {
    helper(1); helper(2);
    let p = Point::new(1, 2);
    p.distance();
}
