import inspect
import json
import os
import typing

VALID_COURSES = [
    "CSE", "ECE", "IT", "AI-ML", "AI-DS",
    "EXTC", "CIVIL", "MECH", "TEXTILE", "PRODUCTION", "SOFTWARE",
]

JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}
PY_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


class Student:
    def __init__(self, name: str, roll_number: int, course: str, marks: int):
        self.name = name
        self.roll_number = roll_number
        self.course = course
        self.marks = marks

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "roll_number": self.roll_number,
            "course": self.course,
            "marks": self.marks,
        }


class StudentDatabase:
    """JSON-backed Student Database Management System with strict domain validation."""

    def __init__(self, file_path: str | None = None):
        if file_path is None:
            file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "students.json")
        self.file_path = file_path
        self.students: list[Student] = []
        self.load_students()
        if not self.students:
            self._seed_sample_data()

    def _seed_sample_data(self):
        self.students = [
            Student("Aarav Sharma", 101, "CSE", 92),
            Student("Priya Nair", 102, "AI-ML", 88),
            Student("Rohan Kulkarni", 103, "IT", 76),
            Student("Ananya Joshi", 104, "AI-DS", 95),
        ]
        self.save_students()

    def save_students(self):
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump([s.to_dict() for s in self.students], f, indent=4)

    def load_students(self):
        self.students = []
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                self.students.append(
                    Student(item["name"], int(item["roll_number"]), item["course"], int(item["marks"]))
                )
        except FileNotFoundError:
            pass

    def roll_number_exists(self, roll_number: int) -> bool:
        return any(s.roll_number == roll_number for s in self.students)

    # --- Read Operations (Safe for Agent Tools) ---

    def view_students(self) -> list[dict]:
        """Return all student records."""
        return [s.to_dict() for s in self.students]

    def search_by_rollnumber(self, roll_number: int) -> dict:
        """Find a single student by roll number."""
        for s in self.students:
            if s.roll_number == roll_number:
                return s.to_dict()
        raise ValueError(f"Student with roll number {roll_number} not found.")

    def calculate_average_marks(self) -> dict:
        """Calculate the average marks across all students."""
        if not self.students:
            return {"count": 0, "average_marks": 0.0}
        avg = sum(s.marks for s in self.students) / len(self.students)
        return {"count": len(self.students), "average_marks": round(avg, 2)}

    def show_top_performers(self) -> list[dict]:
        """Return students with the highest marks."""
        if not self.students:
            return []
        top_score = max(s.marks for s in self.students)
        return [s.to_dict() for s in self.students if s.marks == top_score]

    def sort_by_marks(self) -> list[dict]:
        """Return all students sorted by marks descending."""
        return [s.to_dict() for s in sorted(self.students, key=lambda s: s.marks, reverse=True)]

    def search_by_course_sorted(self, course: str) -> list[dict]:
        """Return students in a given course sorted by marks descending."""
        normalized = course.strip().upper()
        if normalized not in VALID_COURSES:
            raise ValueError(f"Invalid course '{course}'. Allowed: {', '.join(VALID_COURSES)}")
        matches = [s for s in self.students if s.course == normalized]
        return [s.to_dict() for s in sorted(matches, key=lambda s: s.marks, reverse=True)]

    # --- Write Operations (Gated behind Human Approval) ---

    def add_student(self, name: str, roll_number: int, course: str, marks: int) -> dict:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Student name cannot be empty.")
        if isinstance(roll_number, bool) or not isinstance(roll_number, int) or roll_number <= 0:
            raise ValueError("Roll number must be a positive integer.")
        if self.roll_number_exists(roll_number):
            raise ValueError(f"Roll number {roll_number} already exists.")
        clean_course = course.strip().upper()
        if clean_course not in VALID_COURSES:
            raise ValueError(f"Invalid course '{course}'. Allowed: {', '.join(VALID_COURSES)}")
        if isinstance(marks, bool) or not isinstance(marks, int) or not (0 <= marks <= 100):
            raise ValueError("Marks must be an integer between 0 and 100.")

        student = Student(clean_name, roll_number, clean_course, marks)
        self.students.append(student)
        self.save_students()
        return student.to_dict()

    def update_student(self, roll_number: int, marks: int) -> dict:
        if isinstance(marks, bool) or not isinstance(marks, int) or not (0 <= marks <= 100):
            raise ValueError("Marks must be an integer between 0 and 100.")
        for s in self.students:
            if s.roll_number == roll_number:
                s.marks = marks
                self.save_students()
                return s.to_dict()
        raise ValueError(f"Student with roll number {roll_number} not found.")

    def delete_student(self, roll_number: int) -> dict:
        for s in self.students:
            if s.roll_number == roll_number:
                removed = s.to_dict()
                self.students.remove(s)
                self.save_students()
                return {"deleted": removed}
        raise ValueError(f"Student with roll number {roll_number} not found.")


class ToolRegistry:
    """MCP-style tool registry with strict JSON Schema validation."""

    def __init__(self):
        self.tools: dict[str, dict] = {}

    def register(self, fn, description: str | None = None):
        hints = typing.get_type_hints(fn)
        sig = inspect.signature(fn)
        properties = {}
        required = []
        for param_name, param in sig.parameters.items():
            ann = hints.get(param_name)
            properties[param_name] = {"type": JSON_TYPES[ann]} if ann in JSON_TYPES else {}
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
        desc = description or (fn.__doc__.splitlines()[0].strip() if fn.__doc__ else "")
        self.tools[fn.__name__] = {
            "fn": fn,
            "description": desc,
            "inputSchema": {"type": "object", "properties": properties, "required": required},
        }

    def list_tools(self) -> list[dict]:
        return [
            {"name": k, "description": v["description"], "inputSchema": v["inputSchema"]}
            for k, v in self.tools.items()
        ]

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name not in self.tools:
            return {"isError": True, "content": f"Unknown tool: {name}"}
        spec = self.tools[name]
        schema = spec["inputSchema"]
        errors = []
        for req in schema["required"]:
            if req not in arguments:
                errors.append(f"{req}: required")
        for k, v in arguments.items():
            if k not in schema["properties"]:
                errors.append(f"{k}: unknown field")
                continue
            expected = schema["properties"][k].get("type")
            if expected == "integer" and (isinstance(v, bool) or not isinstance(v, int)):
                errors.append(f"{k}: expected integer, got {type(v).__name__}")
            elif expected and expected != "integer" and not isinstance(v, PY_TYPES[expected]):
                errors.append(f"{k}: expected {expected}, got {type(v).__name__}")
        if errors:
            return {"isError": True, "content": "; ".join(errors)}
        try:
            out = spec["fn"](**arguments)
            return {"isError": False, "content": json.dumps(out)}
        except Exception as exc:
            return {"isError": True, "content": f"{type(exc).__name__}: {exc}"}
