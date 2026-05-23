import React from "react";

function helper(x: number): number { return x + 1; }

function Greeter(props: { name: string }) {
  return <div>{helper(1)}</div>;
}

const arrow = (x: number) => <span>{helper(x)}</span>;

helper(1); helper(2);
