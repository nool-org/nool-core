from typing import Annotated

from pydantic import Field

from .common import CamelModel

# An uploaded image only (the admin consoles cap files at 1.5 MB, ~2 MB as base64) -
# never a remote URL, which would ping a third party each time the logo is shown.
LogoDataUri = Annotated[str, Field(max_length=2_100_000,
                                   pattern=r"^data:image/(png|jpeg|jpg|webp|gif|svg\+xml);base64,[A-Za-z0-9+/=\s]+$")]


class SchoolLogoOut(CamelModel):
    """See School.logo_data_uri's docstring. None = no logo set."""

    logo_data_uri: str | None


class UpdateSchoolLogoIn(CamelModel):
    logo_data_uri: LogoDataUri | None
