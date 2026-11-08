class ConfigAttribute:
    def __init__(self, name):
        self.name = name

class App:
    default_config = {
        "DEBUG": False,
        "SECRET_KEY": None,
    }
    secret_key = ConfigAttribute("SECRET_KEY")

    @bp.route("/<int:id>/update", methods=("GET", "POST"))
    def update(self, id):
        return id
