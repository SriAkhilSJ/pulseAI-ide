"""
test_lsp_ast_tools.py
---------------------
TDD Unit Tests for PulseCodeAI LSP Code Intelligence & AST Transformations (`packages/tools/lsp`).
Verifies AST JSDoc insertion and untyped function discovery.
"""
import pytest
from pathlib import Path
from src.lsp_ast_tools import AstAddJsDocTool, AstFindUntypedFunctionsTool, LspGetDiagnosticsTool


def test_ast_find_untyped_functions(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    
    js_code = """
function calculateSum(a, b) {
    return a + b;
}

/**
 * Valid typed function
 * @param {number} x
 */
function typedHelper(x) {
    return x * 2;
}
"""
    (workspace / "math.js").write_text(js_code)

    tool = AstFindUntypedFunctionsTool()
    context = {"workspace_root": str(workspace)}
    res = tool.execute({"path": "math.js"}, context)
    assert res["status"] == "success"
    assert "calculateSum" in res["output"]
    assert "typedHelper" not in res["output"]


def test_ast_add_jsdoc(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    
    js_code = """function multiply(x, y) {\n    return x * y;\n}\n"""
    file_path = workspace / "utils.js"
    file_path.write_text(js_code)

    tool = AstAddJsDocTool()
    context = {"workspace_root": str(workspace)}
    res = tool.execute({"path": "utils.js", "function_name": "multiply"}, context)
    assert res["status"] == "success"
    
    modified = file_path.read_text()
    assert "/**" in modified
    assert "@param" in modified
    assert "multiply(x, y)" in modified


def test_lsp_diagnostics_fallback(tmp_path):
    workspace = tmp_path / "my_project"
    workspace.mkdir()
    (workspace / "app.py").write_text("def run():\n    pass\n")

    tool = LspGetDiagnosticsTool()
    context = {"workspace_root": str(workspace)}
    res = tool.execute({"path": "app.py"}, context)
    assert res["status"] == "success"
    assert "diagnostics" in res["output"].lower() or "clean" in res["output"].lower()
