import React from "react";

function helper(x) { return x + 1; }

function Greeter(props) {
  return <div>{helper(1)}</div>;
}

const arrow = (x) => <span>{helper(x)}</span>;

helper(1); helper(2);
