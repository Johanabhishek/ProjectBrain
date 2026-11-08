def test_login(client):
    client.post("/<int:id>/update", data={"title": "x"})
    assert client.application.secret_key is None
