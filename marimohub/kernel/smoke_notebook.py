"""Disposable acceptance notebook: expected answer 42, no external data/access."""
import marimo

__generated_with = "0.25.1"
app = marimo.App()


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    answer = 6 * 7
    mo.md(f"**Smoke test answer: {answer}**")
    return (answer,)


@app.cell
def _(answer):
    assert answer == 42


if __name__ == "__main__":
    app.run()
