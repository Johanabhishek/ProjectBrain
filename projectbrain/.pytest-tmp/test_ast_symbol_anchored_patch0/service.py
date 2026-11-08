class AccountService:
    def deposit(self, amount: int) -> int:
        if amount <= 0:
            raise ValueError('bad')
        bal = amount + 10
        return bal

    def withdraw(self, amount: int) -> int:
        return -abs(amount)

    def reset(self) -> int:
        return 0
