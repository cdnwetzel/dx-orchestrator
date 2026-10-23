# T-EXAMPLE — temperature converter with a tkinter GUI

<!-- A filled example of templates/spec.md. This exact shape produced a correct,
     consistent result on both a Linux and a macOS box (see LOCAL_NOTES / CHANGELOG).
     Run it:  dx run --required_role frontend-engineer --scope . \
                     --message "$(cat templates/spec.example.md)" -->

- **Role:** `frontend-engineer`
- **Scope:** `.`
- **Base commit:** current HEAD

## 1. Objective

Create `temp_convert.py`: a Celsius/Fahrenheit converter. It must expose two
module-level functions, `c_to_f(c)` and `f_to_c(f)`, each returning a float, and
a tkinter GUI that reads a number from an entry field and shows the converted
value on a label.

## 2. Non-goals

- No Kelvin, no other units.
- No persistence, no settings, no network.
- Do not add a package, a test runner config, or a CLI — one file only.

## 3. Allowed and prohibited paths

- **Allowed:** `temp_convert.py` (new file).
- **Prohibited:** everything else. No changes to existing files.

## 4. Constraints and invariants

- Python standard library only (`tkinter`).
- The GUI must be built only inside `if __name__ == "__main__":`, so the two
  functions are importable without opening a window.
- Window title exactly `dx-under-test`; size 500x300.

## 5. Acceptance criteria and required tests

- `c_to_f(100) == 212`, `c_to_f(0) == 32`, `f_to_c(32) == 0`, `f_to_c(98.6) == 37`.
- Importing the module does not open a window.
- The window shows an entry field, a "C to F" button, an "F to C" button, and a
  result label.

## 6. Reviewer

Chris Wetzel (signs `dx merge` with the reviewer key; must not be the code's author).
