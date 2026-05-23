package main

import (
	"fmt"
	"os"
)

type Person struct {
	Name string
}

func (p *Person) Greet() string { return helper(p.Name) }

func helper(s string) string { return "hi " + s }

func main() {
	_ = os.Args
	_ = fmt.Sprintln("")
	p := &Person{Name: "x"}
	p.Greet()
	helper("a"); helper("b")
}
