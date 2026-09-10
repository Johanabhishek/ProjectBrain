const { findUser } = require("../services/authService");

const handleGetUser = (req, res) => {
  return findUser(req.params.id);
};

app.get("/users/:id", handleGetUser);
