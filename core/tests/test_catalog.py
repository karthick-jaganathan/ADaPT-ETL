"""`streamwright connectors install KEY [KEY ...]` and search: resolution, consent and the single pip command."""

import io
import json
import sys

import pytest

from streamwright.core import catalog, cli

CATALOG = {
    "files": {"title": "Files", "family": "readers", "summary": "Local files", "trust": "official",
              "install": {"git": "git+https://example.test/sw.git@abc#subdirectory=connectors/readers/files"}},
    "google_ads": {"title": "Google Ads", "family": "ads", "summary": "Google Ads reports", "trust": "official",
                   "install": {"git": "git+https://example.test/sw.git@abc#subdirectory=connectors/ads/google_ads"}},
    "meta_ads": {"title": "Meta Ads", "family": "ads", "summary": "Meta Marketing insights", "trust": "official",
                 "install": {"pip": "streamwright-meta-ads>=0.1,<0.2"}},
    "acme": {"title": "Acme", "family": "ads", "summary": "Acme reports", "trust": "community",
             "install": {"git": "git+https://example.test/acme.git@def"}},
    "imaged": {"title": "Imaged", "family": "readers", "summary": "An image", "trust": "official",
               "install": {"image": "ghcr.io/org/imaged@sha256:" + "0" * 64}},
    "empty": {"title": "Empty", "family": "readers", "summary": "No source", "trust": "official", "install": {}},
}


class TTY(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


@pytest.fixture
def pip(monkeypatch):
    """The catalog above; records each pip command instead of running it."""
    calls = []
    monkeypatch.setattr(catalog, "connectors", lambda hub_url=None: CATALOG)
    monkeypatch.setattr(catalog.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
    monkeypatch.setattr(sys, "stdin", TTY(False))
    return calls


def specs(cmd):
    return cmd[cmd.index("install") + 1:]


def test_one_key_installs_its_source(pip):
    assert catalog.install("files") == 0
    assert [specs(cmd) for cmd in pip] == [[CATALOG["files"]["install"]["git"]]]
    assert pip[0][:4] == [sys.executable, "-m", "pip", "install"]


def test_several_keys_install_with_one_pip_command_in_order_without_repeats(pip):
    assert catalog.install_many(["google_ads", "meta_ads", "files", "google_ads"]) == 0
    assert len(pip) == 1
    assert specs(pip[0]) == [CATALOG["google_ads"]["install"]["git"], "streamwright-meta-ads>=0.1,<0.2",
                             CATALOG["files"]["install"]["git"]]


def test_an_unknown_key_installs_nothing(pip, capsys):
    assert catalog.install_many(["google_ads", "nope", "nada"]) == 2
    assert pip == []
    assert "unknown connectors 'nope', 'nada'" in capsys.readouterr().err


def test_a_connector_without_a_source_installs_nothing(pip, capsys):
    assert catalog.install_many(["files", "empty"]) == 2
    assert pip == []
    assert "'empty' has no installable source" in capsys.readouterr().err


def test_no_keys_is_an_error(pip, capsys):
    assert catalog.install_many([]) == 2
    assert pip == []
    assert "at least one connector KEY" in capsys.readouterr().err


def test_a_community_connector_is_refused_non_interactively_without_yes(pip, capsys):
    assert catalog.install_many(["files", "acme"]) == 2
    assert pip == []
    assert "refusing to install community connector 'acme' non-interactively" in capsys.readouterr().err
    assert catalog.install_many(["files", "acme"], assume_yes=True) == 0
    assert specs(pip[0]) == [CATALOG["files"]["install"]["git"], CATALOG["acme"]["install"]["git"]]


def test_one_confirmation_covers_every_untrusted_connector(pip, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", TTY(True))
    answers = []
    monkeypatch.setattr("builtins.input", lambda prompt: answers.append(prompt) or "n")
    assert catalog.install_many(["acme", "files"]) == 1
    assert pip == [] and len(answers) == 1
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert catalog.install_many(["acme", "files"]) == 0
    assert len(pip) == 1
    assert "acme (community): git: git+https://example.test/acme.git@def" in capsys.readouterr().err


def test_images_are_reported_not_installed(pip, capsys):
    assert catalog.install_many(["imaged"]) == 0
    assert pip == []
    assert catalog.install_many(["imaged", "files"]) == 0
    assert specs(pip[0]) == [CATALOG["files"]["install"]["git"]]
    assert "'imaged' is distributed as an image" in capsys.readouterr().out


def test_search_matches_every_word(pip):
    assert set(catalog.search("ads")) == {"google_ads", "meta_ads", "acme"}
    assert set(catalog.search("google ads")) == {"google_ads"}
    assert set(catalog.search("")) == set(CATALOG)


def test_cli_installs_several_connectors(pip):
    assert cli.main(["connectors", "install", "google_ads", "meta_ads", "--yes"]) == 0
    assert specs(pip[0]) == [CATALOG["google_ads"]["install"]["git"], "streamwright-meta-ads>=0.1,<0.2"]


def test_cli_install_needs_a_key(pip, capsys):
    assert cli.main(["connectors", "install"]) == 2
    assert pip == []
    assert "at least one connector KEY" in capsys.readouterr().err


def test_cli_search_joins_its_words(pip, capsys):
    assert cli.main(["connectors", "search", "meta", "insights"]) == 0
    out = capsys.readouterr().out
    assert "meta_ads" in out and "google_ads" not in out


# --- where the catalog comes from: the live hub, else the last fetched copy, else the bundled one ---

GOOD = {"schema_version": 1, "connectors": {"files": CATALOG["files"]}}


class Response:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError("HTTP %d" % self.status)

    def json(self):
        return self.data


@pytest.fixture
def hub(monkeypatch, tmp_path):
    """No $STREAMWRIGHT_HUB_URL, a private cache file, and requests.get answering from `responses` (url -> data)."""
    monkeypatch.delenv(catalog.HUB_ENV, raising=False)
    monkeypatch.setattr(catalog, "CACHE", tmp_path / "connectors.json")
    state = {"responses": {}, "urls": []}

    def get(url, timeout):
        state["urls"].append(url)
        if url not in state["responses"]:
            raise ConnectionError("cannot reach %s" % url)
        return Response(state["responses"][url])
    monkeypatch.setattr("requests.get", get)
    return state


def test_the_live_hub_is_the_default_and_is_cached(hub, capsys):
    hub["responses"][catalog.DEFAULT_HUB] = GOOD
    assert catalog.load() == GOOD
    assert hub["urls"] == [catalog.DEFAULT_HUB]
    assert json.loads(catalog.CACHE.read_text()) == GOOD
    assert capsys.readouterr().err == ""


def test_the_hub_url_can_be_set(hub, monkeypatch):
    hub["responses"]["https://hub.example/a.json"] = GOOD
    hub["responses"]["https://hub.example/b.json"] = GOOD
    monkeypatch.setenv(catalog.HUB_ENV, "https://hub.example/a.json")
    catalog.load()
    catalog.load("https://hub.example/b.json")                    # --hub-url wins over the environment
    assert hub["urls"] == ["https://hub.example/a.json", "https://hub.example/b.json"]


def test_bundled_uses_no_network(hub):
    data = catalog.load(catalog.BUNDLED)
    assert hub["urls"] == [] and data == catalog._bundled()


def test_an_unreachable_hub_falls_back_to_the_last_fetched_copy_with_a_warning(hub, capsys):
    catalog.CACHE.write_text(json.dumps(GOOD))
    assert catalog.load() == GOOD
    err = capsys.readouterr().err
    assert "the connector hub %s is unreachable (ConnectionError" % catalog.DEFAULT_HUB in err
    assert "using the copy fetched on" in err


def test_with_no_fetched_copy_the_bundled_catalog_is_used_with_a_warning(hub, capsys):
    assert catalog.load() == catalog._bundled()
    assert "using the catalog bundled with this release" in capsys.readouterr().err


def test_a_malformed_index_is_not_used_nor_cached(hub, capsys):
    catalog.CACHE.write_text(json.dumps(GOOD))
    hub["responses"][catalog.DEFAULT_HUB] = {"oops": True}
    assert catalog.load() == GOOD                                   # the good copy, not the bad publish
    assert json.loads(catalog.CACHE.read_text()) == GOOD
    assert "not a connector catalog" in capsys.readouterr().err
