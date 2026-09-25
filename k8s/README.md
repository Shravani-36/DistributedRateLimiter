# ☸️ Running on Kubernetes

Same system as `docker compose`, but Kubernetes schedules the pods, restarts
them when they die, and adds more when they get busy.

---

## 🚀 Deploy

```bash
minikube start
minikube addons enable metrics-server        # needed by the HPA

# Build the image INSIDE the cluster's Docker daemon, so there is no
# registry to push to. Run this in the same shell as the build.
eval $(minikube docker-env)                  # PowerShell: minikube docker-env | Invoke-Expression
docker build -t rate-limiter:1.0 .

kubectl apply -f k8s/
kubectl get pods -w                          # wait for 3/3 api pods Running
```

## 🧪 Test it

```bash
API=$(minikube service api --url)

for i in $(seq 1 12); do
  curl -s -D- -o /dev/null -H "X-API-Key: user1" $API/api/data \
    | grep -Ei "^HTTP|x-instance" | tr -d '\r' | paste -sd' '
done
```

➡️ `x-instance` shows a **different pod name** each time, and the 11th request
is still a `429` 🎯 One shared Redis, one shared limit.

## 📈 Watch it scale

```bash
kubectl get hpa api -w                       # in one terminal
BASE_URL=$(minikube service api --url) k6 run loadtest/load_test.js
```

Pods go from 3 → up to 10 as CPU passes 60%.

💡 **The idea worth writing in your README:** more pods raise how much traffic
the API can *handle*; they do **not** raise anyone's rate limit. The limit
lives in Redis and is shared, so 10 pods enforce the same 10 req/min that 3
do. Capacity scales, permission doesn't.

## 🧹 Clean up

```bash
kubectl delete -f k8s/
minikube stop
```

---

## 📦 What's in here

| File | Contains |
|------|----------|
| `redis.yaml` | Redis Deployment + Service — the shared state store |
| `api.yaml` | ConfigMap (limits), API Deployment (3 replicas), NodePort Service |
| `hpa.yaml` | HorizontalPodAutoscaler, 3 → 10 pods on CPU |

---

## 🧠 Decisions worth explaining in an interview

**Liveness is a TCP check, readiness is `/health`.**
Readiness answers "should traffic come here?" — with `FAIL_OPEN=false` and
Redis down, `/health` returns 503 and the pod leaves the Service. Liveness
answers "is this process wedged?" If liveness also pointed at `/health`, a
Redis outage would restart **every pod in a loop** over a dependency being
down. 🔁 Classic outage amplifier, avoided.

**The API pods have a CPU request.**
The HPA scales on CPU *utilisation*, which is measured against the request.
No request → no autoscaling.

**`INSTANCE_NAME` comes from the pod name** via the downward API, so
`X-Instance` in every response tells you which pod answered.

**One Redis replica, on purpose.**
Two would mean two sets of counters and two different limits. Real HA needs
Redis Sentinel or Cluster — a separate project. This is the honest limitation
to name if someone asks "what would you do next?"

---

## ✅ Validation

The manifests are checked against the official Kubernetes 1.31 schemas in
strict mode (unknown fields rejected):

```bash
pip install kubernetes-validate
python -c "
import yaml, glob, kubernetes_validate
for f in glob.glob('k8s/*.yaml'):
    for doc in yaml.safe_load_all(open(f)):
        if doc: kubernetes_validate.validate(doc, '1.31', strict=True)
print('all manifests valid')
"
```
