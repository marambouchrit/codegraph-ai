"""Relationships found in each language: imports, inheritance, calls, uses."""

from app.relationships.models import RelationshipType
from tests.relationship_helpers import analyze, edges, unresolved

IMPORTS, INHERITS = RelationshipType.IMPORTS, RelationshipType.INHERITS
IMPLEMENTS, CALLS = RelationshipType.IMPLEMENTS, RelationshipType.CALLS
USES, DEPENDS_ON = RelationshipType.USES, RelationshipType.DEPENDS_ON


# ----- Python -----

USER_PY = """\
class User:
    def save(self):
        pass

    def login(self):
        self.save()
"""


def test_python_example_from_the_specification() -> None:
    report = analyze(
        {
            "src/models/user.py": USER_PY,
            "src/services/auth.py": """\
from models.user import User

def login():
    user = User()
    user.save()
""",
        }
    )

    assert edges(report) == {
        ("src/services/auth.py", "IMPORTS", "src/models/user.py"),
        ("src/services/auth.py:login", "CALLS", "src/models/user.py:User"),
        ("src/services/auth.py:login", "CALLS", "src/models/user.py:User.save"),
        ("src/models/user.py:User.login", "CALLS", "src/models/user.py:User.save"),
        ("src/services/auth.py", "DEPENDS_ON", "src/models/user.py"),
    }


def test_python_import_forms() -> None:
    report = analyze(
        {
            "app/models/user.py": USER_PY,
            "app/models/__init__.py": "",
            "app/utils.py": "def helper(): pass\n",
            "app/services/auth.py": """\
import app.utils
import app.utils as u
from ..models.user import User as Account
from .. import utils
from app.models import user

def run():
    app.utils.helper()
    u.helper()
    utils.helper()
    Account().save()
    user.User()
""",
        }
    )

    assert edges(report, IMPORTS) == {
        ("app/services/auth.py", "IMPORTS", "app/utils.py"),
        ("app/services/auth.py", "IMPORTS", "app/models/user.py"),
    }
    assert edges(report, CALLS) >= {
        ("app/services/auth.py:run", "CALLS", "app/utils.py:helper"),
        ("app/services/auth.py:run", "CALLS", "app/models/user.py:User"),
    }


def test_python_inheritance_and_inherited_methods() -> None:
    report = analyze(
        {
            "models.py": USER_PY,
            "admin.py": """\
import models
from models import User

class Admin(User):
    def promote(self):
        self.save()
        super().login()

class Root(models.User):
    pass
""",
        }
    )

    assert edges(report, INHERITS) == {
        ("admin.py:Admin", "INHERITS", "models.py:User"),
        ("admin.py:Root", "INHERITS", "models.py:User"),
    }
    # save and login are defined in User, the parent class.
    assert edges(report, CALLS) >= {
        ("admin.py:Admin.promote", "CALLS", "models.py:User.save"),
        ("admin.py:Admin.promote", "CALLS", "models.py:User.login"),
    }


def test_python_calls_in_the_same_file_and_nested_functions() -> None:
    report = analyze(
        {
            "tasks.py": """\
def authenticate():
    pass

def login():
    def check():
        authenticate()
    check()
    login()

authenticate()
"""
        }
    )

    assert edges(report, CALLS) == {
        ("tasks.py:login.check", "CALLS", "tasks.py:authenticate"),
        ("tasks.py:login", "CALLS", "tasks.py:login.check"),
        ("tasks.py:login", "CALLS", "tasks.py:login"),  # recursion is a real call
        ("tasks.py", "CALLS", "tasks.py:authenticate"),  # module-level code: the file calls
    }


def test_python_method_call_does_not_mean_self_call() -> None:
    # Inside a method, save() is a module-level function, never self.save().
    report = analyze(
        {"user.py": "def save(): pass\n\nclass User:\n    def save(self): pass\n    def run(self):\n        save()\n"}
    )

    assert edges(report, CALLS) == {("user.py:User.run", "CALLS", "user.py:save")}


def test_python_annotations_fields_and_variable_types() -> None:
    report = analyze(
        {
            "repo.py": "class Repo:\n    def find(self): pass\n",
            "models.py": USER_PY,
            "service.py": """\
from typing import Optional
from repo import Repo
from models import User

class Service:
    def __init__(self):
        self.repo = Repo()

    def load(self, user: User, other: "User") -> Optional[User]:
        self.repo.find()
        user.save()
        found: Repo = make()
        found.find()
""",
        }
    )

    assert edges(report, USES) == {
        ("service.py:Service.load", "USES", "models.py:User"),
        ("service.py:Service.load", "USES", "repo.py:Repo"),
    }
    assert edges(report, CALLS) >= {
        ("service.py:Service.__init__", "CALLS", "repo.py:Repo"),
        ("service.py:Service.load", "CALLS", "repo.py:Repo.find"),
        ("service.py:Service.load", "CALLS", "models.py:User.save"),
    }
    assert ("service.py:Service.load", "USES", "Optional", "external") in unresolved(report)


def test_python_unresolved_references_are_kept_with_a_reason() -> None:
    report = analyze(
        {
            "main.py": """\
import requests

def run(data):
    requests.get("https://example.com")
    print(data)
    data.items()
"""
        }
    )

    assert edges(report) == set()  # nothing is invented
    assert unresolved(report) == {
        ("main.py", "IMPORTS", "requests", "external"),
        ("main.py:run", "CALLS", "requests.get", "external"),
        ("main.py:run", "CALLS", "print", "not_found"),
        ("main.py:run", "CALLS", "data.items", "unknown_receiver"),
    }


# ----- Java -----

JAVA_USER = """\
package com.example.model;

public class User {
    public void save() {}
    public static User find(String id) { return null; }
}
"""


def test_java_imports_extends_implements_and_calls() -> None:
    report = analyze(
        {
            "src/com/example/model/User.java": JAVA_USER,
            "src/com/example/auth/Authenticatable.java": """\
package com.example.auth;

public interface Authenticatable { void login(); }
""",
            "src/com/example/auth/Admin.java": """\
package com.example.auth;

import com.example.model.User;

public class Admin extends User implements Authenticatable {
    public void login() {
        User user = new User();
        user.save();
        save();
        this.save();
        super.save();
        User.find("root");
    }
}
""",
        }
    )

    admin = "src/com/example/auth/Admin.java"
    user = "src/com/example/model/User.java"
    assert edges(report, IMPORTS) == {(admin, "IMPORTS", user)}
    assert edges(report, INHERITS) == {(f"{admin}:Admin", "INHERITS", f"{user}:User")}
    # Authenticatable is in the same package: no import needed.
    assert edges(report, IMPLEMENTS) == {
        (f"{admin}:Admin", "IMPLEMENTS", "src/com/example/auth/Authenticatable.java:Authenticatable")
    }
    assert edges(report, CALLS) == {
        (f"{admin}:Admin.login", "CALLS", f"{user}:User"),
        (f"{admin}:Admin.login", "CALLS", f"{user}:User.save"),
        (f"{admin}:Admin.login", "CALLS", f"{user}:User.find"),
    }


def test_java_fields_types_and_wildcard_imports() -> None:
    report = analyze(
        {
            "User.java": JAVA_USER,
            "Repo.java": "package com.example.data;\npublic class Repo<T> { public T get() { return null; } }\n",
            "Service.java": """\
package com.example.service;

import com.example.model.*;
import com.example.data.Repo;
import java.util.List;

class Service {
    private Repo<User> repo;

    List<User> all(User filter) {
        repo.get();
        var created = new User();
        created.save();
        return null;
    }
}
""",
        }
    )

    assert edges(report, USES) == {
        ("User.java:User.find", "USES", "User.java:User"),  # its return type
        ("Service.java:Service", "USES", "Repo.java:Repo"),
        ("Service.java:Service", "USES", "User.java:User"),
        ("Service.java:Service.all", "USES", "User.java:User"),
    }
    assert edges(report, CALLS) == {
        ("Service.java:Service.all", "CALLS", "Repo.java:Repo.get"),
        ("Service.java:Service.all", "CALLS", "User.java:User"),
        ("Service.java:Service.all", "CALLS", "User.java:User.save"),
    }
    # The wildcard import links no file; java.util.List is a library.
    assert edges(report, IMPORTS) == {("Service.java", "IMPORTS", "Repo.java")}
    assert ("Service.java", "IMPORTS", "java.util", "external") in unresolved(report)


def test_java_interface_extends_and_static_import() -> None:
    report = analyze(
        {
            "Base.java": "package a;\npublic interface Base {}\n",
            "Util.java": "package a;\npublic class Util { public static int max() { return 1; } }\n",
            "Child.java": """\
package b;

import a.Base;
import static a.Util.max;

interface Child extends Base {}

class Impl implements Child {
    int run() { return max(); }
}
""",
        }
    )

    assert edges(report, INHERITS) == {("Child.java:Child", "INHERITS", "Base.java:Base")}
    assert edges(report, IMPLEMENTS) == {("Child.java:Impl", "IMPLEMENTS", "Child.java:Child")}
    assert edges(report, CALLS) == {("Child.java:Impl.run", "CALLS", "Util.java:Util.max")}


def test_java_overloaded_methods_are_ambiguous() -> None:
    report = analyze(
        {
            "A.java": """\
class A {
    void log() {}
    void log(String message) {}
    void run() { log(); }
}
"""
        }
    )

    assert edges(report, CALLS) == set()
    assert ("A.java:A.run", "CALLS", "log", "ambiguous") in unresolved(report)


# ----- JavaScript -----


def test_javascript_imports_extends_and_calls() -> None:
    report = analyze(
        {
            "src/models/User.js": """\
export default class User {
  save() {}
}
export function createUser() { return new User(); }
""",
            "src/admin.js": """\
import Account, { createUser as make } from "./models/User";
import * as users from "./models/User.js";
import React from "react";

class Admin extends Account {
  promote() {
    this.save();
    super.save();
    make();
    users.createUser();
    const other = new Account();
    other.save();
  }
}
""",
        }
    )

    assert edges(report, IMPORTS) == {("src/admin.js", "IMPORTS", "src/models/User.js")}
    assert edges(report, INHERITS) == {("src/admin.js:Admin", "INHERITS", "src/models/User.js:User")}
    assert edges(report, CALLS) == {
        ("src/admin.js:Admin.promote", "CALLS", "src/models/User.js:User.save"),
        ("src/admin.js:Admin.promote", "CALLS", "src/models/User.js:createUser"),
        ("src/admin.js:Admin.promote", "CALLS", "src/models/User.js:User"),
        ("src/models/User.js:createUser", "CALLS", "src/models/User.js:User"),
    }
    assert ("src/admin.js", "IMPORTS", "react", "external") in unresolved(report)


def test_javascript_commonjs_arrow_functions_and_jsx() -> None:
    report = analyze(
        {
            "lib/helpers.js": "function format() {}\nmodule.exports = { format };\n",
            "components/Card.jsx": "export function Card() { return <div />; }\n",
            "app.jsx": """\
const helpers = require("./lib/helpers");
import { Card } from "./components/Card";
import "./styles.css";

const render = () => {
  helper();
  return <Card title="x" />;
};

function helper() {}
""",
        }
    )

    assert edges(report, IMPORTS) == {
        ("app.jsx", "IMPORTS", "lib/helpers.js"),
        ("app.jsx", "IMPORTS", "components/Card.jsx"),
    }
    assert edges(report, CALLS) == {("app.jsx:render", "CALLS", "app.jsx:helper")}
    assert edges(report, USES) == {("app.jsx:render", "USES", "components/Card.jsx:Card")}
    assert ("app.jsx", "IMPORTS", "./styles.css", "external") in unresolved(report)


def test_javascript_index_files_and_re_exports() -> None:
    report = analyze(
        {
            "models/user.js": "export class User { save() {} }\n",
            "models/index.js": 'export { User } from "./user";\n',
            "main.js": 'import { User } from "./models";\nnew User().save;\nconst u = new User();\nu.save();\n',
        }
    )

    assert edges(report, IMPORTS) == {
        ("models/index.js", "IMPORTS", "models/user.js"),
        ("main.js", "IMPORTS", "models/index.js"),
    }
    assert edges(report, CALLS) == {
        ("main.js", "CALLS", "models/user.js:User"),
        ("main.js", "CALLS", "models/user.js:User.save"),
    }


# ----- TypeScript -----


def test_typescript_imports_extends_implements_and_interfaces() -> None:
    report = analyze(
        {
            "src/auth.ts": """\
export interface Identifiable { id(): string; }
export interface Authenticatable extends Identifiable { login(): void; }
""",
            "src/base.ts": "export abstract class BaseService { protected log(): void {} }\n",
            "src/user-service.ts": """\
import { Authenticatable } from "./auth";
import { BaseService } from "./base";
import type { Identifiable } from "./auth";

export class UserService extends BaseService implements Authenticatable {
  id(): string { return "1"; }
  login(): void {
    this.log();
    this.id();
  }
}
""",
        }
    )

    service = "src/user-service.ts"
    assert edges(report, IMPORTS) == {
        (service, "IMPORTS", "src/auth.ts"),
        (service, "IMPORTS", "src/base.ts"),
    }
    assert edges(report, INHERITS) == {
        (f"{service}:UserService", "INHERITS", "src/base.ts:BaseService"),
        ("src/auth.ts:Authenticatable", "INHERITS", "src/auth.ts:Identifiable"),
    }
    assert edges(report, IMPLEMENTS) == {
        (f"{service}:UserService", "IMPLEMENTS", "src/auth.ts:Authenticatable")
    }
    assert edges(report, CALLS) == {
        (f"{service}:UserService.login", "CALLS", "src/base.ts:BaseService.log"),
        (f"{service}:UserService.login", "CALLS", f"{service}:UserService.id"),
    }


def test_typescript_types_fields_and_parameter_properties() -> None:
    report = analyze(
        {
            "repo.ts": "export class Repo<T> { get(): T { return null as any; } }\n",
            "user.ts": "export class User { save(): void {} }\n",
            "service.tsx": """\
import { Repo } from "./repo";
import { User } from "./user";

export class Service {
  private users: Repo<User>;
  constructor(private readonly repo: Repo<User>) {}

  load(user: User): Promise<User> {
    this.repo.get();
    this.users.get();
    user.save();
    const other: User = find();
    other.save();
    return null as any;
  }
}
""",
        }
    )

    assert edges(report, USES) == {
        ("service.tsx:Service", "USES", "repo.ts:Repo"),
        ("service.tsx:Service", "USES", "user.ts:User"),
        ("service.tsx:Service.constructor", "USES", "repo.ts:Repo"),
        ("service.tsx:Service.constructor", "USES", "user.ts:User"),
        ("service.tsx:Service.load", "USES", "user.ts:User"),
    }
    assert edges(report, CALLS) == {
        ("service.tsx:Service.load", "CALLS", "repo.ts:Repo.get"),
        ("service.tsx:Service.load", "CALLS", "user.ts:User.save"),
    }
    assert ("service.tsx:Service.load", "USES", "Promise", "not_found") in unresolved(report)
