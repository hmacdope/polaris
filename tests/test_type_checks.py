import pytest

from polaris._artifact import BaseArtifactModel
from polaris.utils.types import HubOwner


def test_slug_compatible_string_type():
    """Verifies that the artifact name is validated correctly."""

    # Fails if:
    # - Is too short (<4 characters)
    # - Is too long (>64 characters)
    # - Contains non alpha-numeric characters
    for name in ["", "x", "xx", "xxx", "x" * 65, "invalid@", "invalid!"]:
        with pytest.raises(ValueError):
            BaseArtifactModel(name=name)
        with pytest.raises(ValueError):
            HubOwner(userId=name)

    # Does not fail
    for name in ["valid", "valid-name", "valid_name", "ValidName1", "Valid_", "Valid-", "x" * 64, "x" * 4]:
        BaseArtifactModel(name=name)
        HubOwner(userId=name)


def test_artifact_owner():
    with pytest.raises(ValueError):
        # No owner specified
        HubOwner()
    with pytest.raises(ValueError):
        # Conflicting owner specified
        HubOwner(organizationId="org", userId="user")

    # Valid - Only specifies one!
    assert HubOwner(organizationId="org").owner == "org"
    assert HubOwner(userId="user").owner == "user"
