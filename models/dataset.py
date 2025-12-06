from api.database import db

class DataSet(db.Model):
    __tablename__ = "dataset"
    id    = db.Column(db.String, primary_key=True)
    label = db.Column(db.String, nullable=False)
