import os
import httpx
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

class N8nClientError(RuntimeError):
    """Raised when n8n is configured but the API cannot be queried."""


class N8nClient:
    def __init__(self):
        self.base_url = os.getenv("N8N_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("N8N_API_KEY", "")
        
        timeout_str = os.getenv("N8N_TIMEOUT_SECONDS", "10")
        try:
            self.timeout = int(timeout_str)
        except ValueError:
            self.timeout = 10
            
        self.configured = bool(self.base_url)

    def _headers(self) -> Dict[str, str]:
        if not self.api_key:
            return {}
        return {"X-N8N-API-KEY": self.api_key}

    async def health(self) -> Dict[str, Any]:
        """Check health against N8N_BASE_URL/healthz."""
        if not self.configured:
            return {"configured": False, "status": "unknown"}
            
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.base_url}/healthz")
                # n8n /healthz returns 200 with {"status":"ok"}
                is_ok = resp.status_code == 200
                return {
                    "configured": True,
                    "status": "ok" if is_ok else "error",
                    "http_status": resp.status_code
                }
        except Exception as e:
            logger.error(f"n8n health check failed: {e}")
            return {
                "configured": True,
                "status": "error",
                "http_status": "unreachable",
                "error": str(e)
            }

    async def list_workflows(self) -> List[Dict[str, Any]]:
        """List all workflows."""
        if not self.configured:
            return []

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.base_url}/api/v1/workflows",
                    headers=self._headers()
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("data", [])
        except Exception as e:
            logger.error(f"n8n list_workflows failed: {e}")
            raise N8nClientError(f"n8n list_workflows failed: {e}") from e

    async def list_executions(self, status: str | None = "error", limit: int = 10) -> List[Dict[str, Any]]:
        """List executions, optionally filtered by status."""
        if not self.configured:
            return []

        try:
            params: Dict[str, Any] = {"limit": limit}
            if status:
                params["status"] = status
                
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.base_url}/api/v1/executions",
                    headers=self._headers(),
                    params=params
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("data", [])
        except Exception as e:
            logger.error(f"n8n list_executions failed: {e}")
            raise N8nClientError(f"n8n list_executions failed: {e}") from e

    async def get_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Get a single workflow by ID."""
        if not self.configured:
            return {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.base_url}/api/v1/workflows/{workflow_id}",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.error("n8n get_workflow %s failed: %s", workflow_id, e)
            raise N8nClientError(f"n8n get_workflow failed: {e}") from e

    async def get_workflow_executions(self, workflow_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Get recent executions for a specific workflow."""
        if not self.configured:
            return []
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.base_url}/api/v1/executions",
                    headers=self._headers(),
                    params={"workflowId": workflow_id, "limit": limit},
                )
                resp.raise_for_status()
                return resp.json().get("data", [])
        except Exception as e:
            logger.error("n8n get_workflow_executions %s failed: %s", workflow_id, e)
            raise N8nClientError(f"n8n get_workflow_executions failed: {e}") from e

    async def retry_execution(self, execution_id: str) -> Dict[str, Any]:
        """Retry a failed execution by ID."""
        if not self.configured:
            raise N8nClientError("n8n is not configured")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/api/v1/executions/{execution_id}/retry",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.error("n8n retry_execution %s failed: %s", execution_id, e)
            raise N8nClientError(f"n8n retry_execution failed: {e}") from e

    async def deactivate_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Deactivate (pause) a workflow by ID."""
        if not self.configured:
            raise N8nClientError("n8n is not configured")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/api/v1/workflows/{workflow_id}/deactivate",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.error("n8n deactivate_workflow %s failed: %s", workflow_id, e)
            raise N8nClientError(f"n8n deactivate_workflow failed: {e}") from e

    async def get_failed_executions_summary(self, limit: int = 10) -> Dict[str, Any]:
        """Get a summary of failed executions."""
        if not self.configured:
            return {"configured": False, "status": "unknown"}

        try:
            executions = await self.list_executions(status="error", limit=limit)
        except N8nClientError as e:
            return {
                "configured": True,
                "status": "error",
                "failed_count": None,
                "executions": [],
                "error": str(e),
            }

        count = len(executions)
        return {
            "configured": True,
            "status": "ok" if count == 0 else "error",
            "failed_count": count,
            "executions": executions
        }
