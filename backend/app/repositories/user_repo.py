from app.models.user import User
from app.repositories.base import BaseRepository


class UserRepo(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        return await self.session.scalar(self.select(User.email == email))
