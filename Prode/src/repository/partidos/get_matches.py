from infrastructure.db_conn.mysql_config import get_connection

def get_partidos_repo(equipo: str, fecha: str, fase: str, limit: int, offset: int):
    conn = get_connection()

    try:
        with conn.cursor() as cursor:

            query_filtros = ""
            valores = []

            if equipo:
                query_filtros += " AND (equipo_local LIKE %s OR equipo_visitante LIKE %s)"
                valores.extend([f"%{equipo}%", f"%{equipo}%"])

            if fecha:
                query_filtros += " AND DATE(fecha) = %s"
                valores.append(fecha)

            if fase:
                query_filtros += " AND fase = %s"
                valores.append(fase)

            query_total = f"""
                SELECT COUNT(*) AS total
                FROM fixtures
                WHERE 1=1 {query_filtros}
            """

            cursor.execute(query_total, tuple(valores))

            total = int(cursor.fetchone()["total"])

            query_datos = f"""
                SELECT
                    id,
                    equipo_local,
                    equipo_visitante,
                    fecha,
                    fase
                FROM fixtures
                WHERE 1=1 {query_filtros}
                LIMIT %s OFFSET %s
            """

            valores_finales = valores + [limit, offset]

            cursor.execute(query_datos, tuple(valores_finales))

            rows = cursor.fetchall()

            items = []

            for row in rows:
                items.append({
                    "id": row["id"],
                    "equipo_local": row["equipo_local"],
                    "equipo_visitante": row["equipo_visitante"],
                    "fecha": str(row["fecha"]),
                    "fase": row["fase"],
                })

            return items, total

    except Exception as e:
        print("ERROR MYSQL:", e)
        return None, 0

    finally:
        conn.close()
