import httpx

from agent.discover import company_discovery


def test_generate_slugs():
    slugs = company_discovery.generate_slugs("Razorpay Software Private Limited")
    assert "razorpaysoftware" in slugs or "razorpay" in slugs
    slugs_hyphen = company_discovery.generate_slugs("Alpha Beta Technologies")
    assert "alphabeta" in slugs_hyphen or "alpha-beta" in slugs_hyphen or "alpha" in slugs_hyphen
    assert company_discovery.generate_slugs("") == []


def test_is_india_location():
    assert company_discovery.is_india_location("Bengaluru, India")
    assert company_discovery.is_india_location("Remote (IN)")
    assert company_discovery.is_india_location("Hyderabad, Telangana")
    assert not company_discovery.is_india_location("San Francisco, CA")
    assert not company_discovery.is_india_location("London, UK")
    assert not company_discovery.is_india_location(None)


def test_jaccard_similarity():
    sim = company_discovery.jaccard_similarity("Acme Technologies India", "Acme Technologies Global")
    assert sim >= 0.5
    assert company_discovery.jaccard_similarity("Acme", "Acme") == 1.0
    assert company_discovery.jaccard_similarity("Acme", "Zeta") == 0.0


def test_probe_greenhouse():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs":
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 1,
                            "title": "Backend Engineer",
                            "location": {"name": "Bengaluru, India"},
                        }
                    ]
                },
            )
        if url == "https://boards-api.greenhouse.io/v1/boards/acme":
            return httpx.Response(200, json={"name": "Acme Technologies"})
        return httpx.Response(404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    status, matched, board_name = company_discovery.probe_greenhouse(client, "acme", "Acme Technologies")
    assert status == 200 and matched is True and board_name == "Acme Technologies"

    status_404, matched_404, _ = company_discovery.probe_greenhouse(client, "nonexistent", "Acme")
    assert status_404 == 404 and matched_404 is False


def test_probe_lever():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://api.lever.co/v0/postings/beta?mode=json":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "1",
                        "text": "Full Stack Engineer",
                        "categories": {"location": "Bengaluru"},
                        "country": "IN",
                    }
                ],
            )
        return httpx.Response(404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    status, matched = company_discovery.probe_lever(client, "beta", ["Full Stack Engineer"])
    assert status == 200 and matched is True

    status_nomatch, matched_nomatch = company_discovery.probe_lever(client, "beta", ["Unrelated Role XYZ"])
    assert status_nomatch == 200 and matched_nomatch is False


def test_probe_ashby():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://api.ashbyhq.com/posting-api/job-board/gamma":
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": "1",
                            "title": "AI Platform Engineer",
                            "location": "Remote, India",
                            "isListed": True,
                        }
                    ]
                },
            )
        return httpx.Response(404, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    status, matched = company_discovery.probe_ashby(client, "gamma", ["AI Platform Engineer"])
    assert status == 200 and matched is True
