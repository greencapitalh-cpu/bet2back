import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, Blueprint
from flask_cors import CORS

from infrastructure.entrypoints.users import users
from infrastructure.entrypoints.partidos import partidos
from infrastructure.entrypoints.ranking.ranking import get_ranking
from infrastructure.entrypoints.mvp_api import mvp_api

app = Flask(__name__)
CORS(app, supports_credentials=True)

users_bp = Blueprint('users', __name__, url_prefix='/users')
partidos_bp = Blueprint('partidos', __name__, url_prefix='/partidos')
ranking_bp = Blueprint('ranking', __name__, url_prefix='/ranking')


@app.route('/healthz', methods=['GET'])
def healthcheck():
    return {'status': 'ok'}, 200


@users_bp.route('/register', methods=['POST'])
def create_user_endpoint():
    return users.create_user()


@users_bp.route('/<int:user_id>', methods=['PUT'])
def update_user_endpoint(user_id):
    return users.update_user(user_id)


@users_bp.route('/<int:user_id>', methods=['GET'])
def get_user_endpoint(user_id: int):
    return users.get_user(user_id)


@users_bp.route('/<int:user_id>', methods=['DELETE'])
def delete_user_endpoint(user_id):
    return users.delete_usuario(user_id)


@app.route('/usuarios', methods=['GET'])
def get_users_list_endpoint():
    return users.get_users_list()


@partidos_bp.route('', methods=['GET'])
def get_partidos_endpoint():
    return partidos.get_partidos()


@partidos_bp.route('/<int:partido_id>', methods=['GET'])
def get_partido_by_id_endpoint(partido_id: int):
    return partidos.get_partido_by_id(partido_id)


@partidos_bp.route('/<int:partido_id>/resultado', methods=['PUT'])
def put_resultado_endpoint(partido_id: int):
    return partidos.put_resultado(partido_id)


@partidos_bp.route('/<int:partido_id>', methods=['PUT'])
def put_replace_partido_endpoint(partido_id: int):
    return partidos.put_replace_partido(partido_id)


@partidos_bp.route('/<int:partido_id>', methods=['DELETE'])
def delete_partido_endpoint(partido_id: int):
    return partidos.delete_partido(partido_id)


@partidos_bp.route('/<int:partido_id>/prediccion', methods=['POST'])
def post_prediccion_endpoint(partido_id: int):
    return partidos.post_prediccion(partido_id)


@partidos_bp.route('', methods=['POST'])
def create_partido_endpoint():
    return partidos.post_partido()


@ranking_bp.route('/', methods=['GET'], strict_slashes=False)
def get_ranking_endpoint():
    return get_ranking()


app.register_blueprint(users_bp)
app.register_blueprint(partidos_bp)
app.register_blueprint(ranking_bp)
app.register_blueprint(mvp_api)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
