const { handleGetUser } = require("./routes/users");

function createApp() {
  return handleGetUser;
}

module.exports = { createApp };
