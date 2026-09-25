#!/usr/bin/env python3
"""Pin the verify_signature function body: line range, byte range, sha256 of the
body (def..end, decorators excluded, LF, no trailing newline) and of the whole file.
Usage: python3 pin_verify_signature.py <path/to/server.py>   (stdlib only)"""
import ast, hashlib, sys
def main(path):
    data=open(path,"rb").read(); text=data.decode("utf-8"); lines=text.split("\n")
    tree=ast.parse(text)
    node=next((n for n in ast.walk(tree)
               if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=="verify_signature"),None)
    if node is None: print("verify_signature not found in",path); return 1
    d,e=node.lineno,node.end_lineno                      # def..end, decorators excluded
    body="\n".join(lines[d-1:e])
    start=len(("\n".join(lines[:d-1])+("\n" if d-1>0 else "")).encode("utf-8"))
    end=start+len(body.encode("utf-8"))
    print(f"file: {path}")
    print(f"  line range: def L{d}..end L{e} (decorators excluded)")
    print(f"  byte range: start {start}, end {end} (exclusive)")
    print(f"  body sha256: {hashlib.sha256(body.encode('utf-8')).hexdigest()}")
    print(f"  file sha256: {hashlib.sha256(data).hexdigest()}")
    return 0
if __name__=="__main__":
    sys.exit(main(sys.argv[1]))
