const { findUserInDb } = require("../models/user");

function findUser(id) {
  return findUserInDb(id);
}

module.exports = { findUser };
