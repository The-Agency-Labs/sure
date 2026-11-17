import os
import stat

import pytest

from ingest.secrets import DirStore, SecretError, SystemdCredsStore, file_name


def test_file_name():
    assert file_name("simplii/password") == "simplii.password"
    for bad in ("simplii", "Simplii/password", "../x/password", "simplii/pass.word", "simplii/"):
        with pytest.raises(SecretError):
            file_name(bad)


def test_dir_store(tmp_path):
    (tmp_path / "simplii.username").write_text("4500 0000")
    (tmp_path / "simplii.empty").write_text("")
    store = DirStore(tmp_path)
    assert store.get("simplii/username") == "4500 0000"
    with pytest.raises(SecretError, match="set-credential.sh simplii password"):
        store.get("simplii/password")
    with pytest.raises(SecretError, match="empty"):
        store.get("simplii/empty")
    store.close()


def test_systemd_creds_store_uses_the_file_name_as_credential_name(tmp_path):
    # Stand-in for systemd-creds: checks --name matches the file, prints a value.
    fake = tmp_path / "fake-creds"
    fake.write_text(
        "#!/bin/sh\n"
        '[ "$1" = decrypt ] || exit 9\n'
        'n=${2#--name=}; [ "$(basename "$3" .cred)" = "$n" ] || { echo "name mismatch" >&2; exit 1; }\n'
        'printf "value-of-%s" "$n"\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    creds = tmp_path / "creds"
    creds.mkdir()
    for n in ("simplii.username", "simplii.password", "amex.username"):
        (creds / f"{n}.cred").write_bytes(b"x")
    store = SystemdCredsStore(creds, binary=str(fake))
    assert store.get("simplii/password") == "value-of-simplii.password"
    assert store.all_values() == ["value-of-amex.username", "value-of-simplii.password", "value-of-simplii.username"]
    with pytest.raises(SecretError, match="no credential"):
        store.get("amex/password")


def test_systemd_creds_store_reports_decrypt_failure(tmp_path):
    fake = tmp_path / "fail"
    fake.write_text("#!/bin/sh\necho 'bad key' >&2\nexit 1\n")
    os.chmod(fake, 0o755)
    (tmp_path / "simplii.password.cred").write_bytes(b"x")
    with pytest.raises(SecretError, match="could not decrypt simplii.password.cred: bad key"):
        SystemdCredsStore(tmp_path, binary=str(fake)).get("simplii/password")
