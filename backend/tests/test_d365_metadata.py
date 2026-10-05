import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.d365.auth import D365AuthManager
from app.integrations.d365.client import D365ODataClient
from app.integrations.d365.metadata import D365MetadataResolver


METADATA = """<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><edmx:DataServices><Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="Microsoft.Dynamics.DataEntities" Alias="D365"><EntityType Name="Base"><Key><PropertyRef Name="dataAreaId"/><PropertyRef Name="Voucher"/></Key><Property Name="dataAreaId" Type="Edm.String"/><Property Name="CustomerAccount" Type="Edm.String"/><Property Name="CurrencyCode" Type="Edm.String"/><Property Name="InvoiceNumber" Type="Edm.String"/><Property Name="TransactionDate" Type="Edm.DateTimeOffset"/><Property Name="DueDate" Type="Edm.DateTimeOffset"/><Property Name="Voucher" Type="Edm.String"/></EntityType><EntityType Name="Open" BaseType="D365.Base"><Property Name="RemainingAmount" Type="Edm.Decimal"/><Property Name="Amount" Type="Edm.Decimal"/></EntityType><EntityType Name="Transaction" BaseType="D365.Base"><Property Name="AmountCur" Type="Edm.Decimal"/><Property Name="TransactionType" Type="Edm.String"/></EntityType><EntityContainer Name="Data"><EntitySet Name="CustomerOpenTransactions" EntityType="D365.Open"/><EntitySet Name="CustomerTransactions" EntityType="D365.Transaction"/><EntitySet Name="VendorOpenTransactions" EntityType="D365.Open"/><EntitySet Name="CustomerOpenTransactionsA" EntityType="D365.Open"/></EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"""


def resolver(settings=None, client=None):
    return D365MetadataResolver(client, settings or Settings(_env_file=None))


def test_metadata_parses_namespace_alias_inherited_properties_and_keys():
    metadata = resolver()
    entities = metadata.parse(METADATA)
    assert entities["CustomerOpenTransactions"].resolve("remaining") == "RemainingAmount"
    assert entities["CustomerOpenTransactions"].resolve("account") == "CustomerAccount"
    assert entities["CustomerOpenTransactions"].keys == ["dataAreaId", "Voucher"]
    assert metadata.score(entities["VendorOpenTransactions"], "open_transactions") == 0
    assert metadata.score(entities["CustomerTransactions"], "open_transactions") == 0
    assert metadata.score(entities["CustomerOpenTransactions"], "customer_transactions") == 0


@pytest.mark.asyncio
async def test_auto_candidates_are_deterministic_and_validated():
    calls = []

    def transport(request):
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "test", "expires_in": 3600})
        calls.append(request)
        if request.url.path.endswith("$metadata"):
            return httpx.Response(200, text=METADATA)
        assert request.url.params["$top"] == "1"
        if request.url.path.endswith("CustomerOpenTransactions"):
            return httpx.Response(403)
        return httpx.Response(200, json={"value": []})

    settings = Settings(
        _env_file=None, d365_tenant_id="test", d365_client_id="test", d365_client_secret="test"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, D365AuthManager(settings, http), http)
        metadata = resolver(settings, client)
        registry = await metadata.load()
        assert registry.resolved["open_transactions"] == "CustomerOpenTransactionsA"
        assert registry.resolved["customer_transactions"] == "CustomerTransactions"
        assert registry.candidates["open_transactions"][0]["entity"] == "CustomerOpenTransactions"
        assert any(item.url.path.endswith("CustomerOpenTransactionsA") for item in calls)


@pytest.mark.asyncio
async def test_explicit_unknown_entity_leaves_capability_unavailable():
    class Client:
        async def request(self, *args):
            return httpx.Response(200, text=METADATA)

        async def get(self, *args, **kwargs):
            return []

    settings = Settings(_env_file=None, d365_open_transactions_entity="EntityDoesNotExist")
    registry = await resolver(settings, Client()).load()
    assert registry.resolved["open_transactions"] is None
    assert "D365_OPEN_TRANSACTIONS_ENTITY" in registry.messages[0]


def test_metadata_rejects_dtd_and_malformed_xml():
    for body in ('<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><foo/>', "<invalid"):
        with pytest.raises(AppError):
            resolver().parse(body)
