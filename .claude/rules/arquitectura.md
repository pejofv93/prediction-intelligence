# Reglas de Arquitectura NEXUS

## Interfaz obligatoria de agentes
class NombreAgente:
    def __init__(self, config: dict, db: DBManager):
        self.logger = get_logger("NOMBRE_AGENTE")
        self.config = config
        self.db = db

    def run(self, ctx: Context) -> Context:
        self.logger.info("NOMBRE_AGENTE iniciado")
        try:
            # lógica del agente
            pass
        except Exception as e:
            self.logger.error(f"Error: {e}")
            ctx.errors.append(str(e))
        return ctx

## Context
Contiene TODOS los datos del pipeline.
Nunca pasar datos entre agentes de otra forma.

## Base de datos
- Usar SIEMPRE DBManager de database/db.py
- Nunca SQL directo fuera de db.py
- Nunca archivos JSON para persistencia
- Toda persistencia va a SQLite

## Errores
- try/except en todo run()
- En error: ctx.errors.append(error), return ctx
- Nunca raise sin capturar
- Siempre loguear con self.logger.error()
