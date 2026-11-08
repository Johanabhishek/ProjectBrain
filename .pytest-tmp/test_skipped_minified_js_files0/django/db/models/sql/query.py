class Query:
    def build_filter(self, filter_expr, branch_negated=False):
        arg, value = filter_expr
        return (arg, value, branch_negated)
