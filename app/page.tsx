import { requireChatGPTUser } from "./chatgpt-auth";
import Workspace from "./workspace";
import {speechExecution} from "@/lib/runtime";
import {processingConfig} from "@/lib/processing";
export const dynamic="force-dynamic";
export default async function Home(){const user=await requireChatGPTUser("/");return <Workspace displayName={user.displayName} initialConfig={processingConfig(speechExecution())}/>;}
