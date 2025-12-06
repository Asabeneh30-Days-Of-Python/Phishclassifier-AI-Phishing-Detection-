def test_rate_limiting(client):
    # Simulate multiple rapid requests to trigger the rate limiting mechanism.
    for _ in range(50):
        response = client.get("/api/secure-endpoint")
    # Expect a 429 status code when the request threshold is met.
    assert response.status_code == 429

