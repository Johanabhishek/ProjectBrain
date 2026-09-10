from models.user import User


class Admin(User):
    def promote(self):
        return True
